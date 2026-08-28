"""Master-data upload: validate a single-dimension file and load it.

The Phase 1 importer (``master_data.importer``) loads the *whole workbook*: it
requires every sheet, validates them together, and upserts them in hierarchy
order. That is the right tool for the official ``Master Data.xlsx`` and it is
left exactly as it is.

This module handles the other case the upload centre needs: **one dimension at a
time, from a file a user just picked**. It reuses the same schema
(``master_data.schema``), the same cleaning rules (``utils.cleaning._clean_value``
via :func:`clean_value`) and the same identity rule (the official business code
is the key, and is never rewritten). What it adds is per-row validation with a
row number, a column and a suggested fix, so the person who prepared the file can
correct it.

Nothing is deleted. ``INSERT`` refuses to touch an existing code, ``UPDATE``
refuses to create a new one, ``UPSERT`` does both — and none of the three
removes a row that the file happens to omit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database.models import plant_key, storage_location_key
from ..etl.bulk import bulk_insert
from ..database.models_admin import ImportMode
from ..database.models_warehouse import (
    STATUS_AVAILABLE,
    MasterSourceStatus,
)
from ..etl.readers import SourceReader, SourceRow
from ..master_data.schema import FieldKind
from ..utils.cleaning import _clean_value
from ..utils.progress import MASTER_SCALE, Phase, ProgressReporter
from ..utils.text import is_blank, snake_case
from .errors import UploadIssue, code
from .registry import MASTER_MODEL_BY_TABLE, UploadColumn, UploadType

#: Master upload keys whose model is a Phase 1 hierarchy dimension with a parent.
_KIND_BY_NAME = {kind.value: kind for kind in FieldKind}

#: Joined-key column -> the function that builds it from the row's key columns.
#:
#: A composite-key master stores its natural key joined into one column so the
#: fact table can reference one column and an upsert can conflict on one. The
#: builder is the same function the stock ETL and the reporting join call, so
#: the three cannot disagree on spelling or separator, and it is applied
#: positionally to ``UploadType.business_key`` — which is why the order declared
#: in ``registry._COMPOSITE_KEYS`` is the order these functions expect.
COMPOSITE_KEY_BUILDERS = {
    "plant_key": plant_key,
    "storage_location_key": storage_location_key,
}


@dataclass
class MasterRow:
    """One source row, cleaned and judged."""

    row_number: int
    raw: dict[str, Any]
    values: dict[str, Any] = field(default_factory=dict)
    issues: list[UploadIssue] = field(default_factory=list)
    #: INSERT / UPDATE / DUPLICATE / SKIP — decided once, applied later.
    action: str = "INSERT"

    @property
    def ok(self) -> bool:
        return not self.issues


@dataclass
class MasterValidation:
    """The verdict on a whole master file."""

    upload_type: UploadType
    headers: list[str] = field(default_factory=list)
    unmapped_columns: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    rows: list[MasterRow] = field(default_factory=list)
    issues: list[UploadIssue] = field(default_factory=list)
    fatal: str | None = None

    @property
    def total_rows(self) -> int:
        return len(self.rows)

    @property
    def valid_rows(self) -> list[MasterRow]:
        return [row for row in self.rows if row.ok and row.action != "DUPLICATE"]

    @property
    def invalid_row_numbers(self) -> set[int]:
        return {row.row_number for row in self.rows if row.issues}

    @property
    def duplicate_rows(self) -> int:
        return sum(1 for row in self.rows if row.action == "DUPLICATE")

    @property
    def all_issues(self) -> list[UploadIssue]:
        collected = list(self.issues)
        for row in self.rows:
            collected.extend(row.issues)
        return collected


def _header_index(upload_type: UploadType, headers: list[str]) -> dict[str, str]:
    """``{source header: target column}`` for the headers a file actually has.

    Matching is tolerant of case, spacing and punctuation (``"BU Code"``,
    ``"bu_code"`` and ``"bu code"`` all resolve), because a file exported from
    another system rarely reproduces the template's exact capitalisation.
    """
    lookup: dict[str, str] = {}
    for column in upload_type.columns:
        for candidate in (column.name, column.target, *column.aliases):
            lookup[snake_case(candidate)] = column.target

    mapping: dict[str, str] = {}
    for header in headers:
        if header is None:
            continue
        target = lookup.get(snake_case(str(header)))
        if target is not None and target not in mapping.values():
            mapping[str(header)] = target
    return mapping


def clean_value(column: UploadColumn, raw: Any) -> tuple[Any, str | None]:
    """Coerce one cell using the Phase 1 cleaning rules."""
    kind = _KIND_BY_NAME.get(column.kind, FieldKind.TEXT)

    class _Spec:            # the cleaner only reads ``.kind``
        pass

    spec = _Spec()
    spec.kind = kind
    return _clean_value(spec, raw)


def validate(session: Session, upload_type: UploadType, reader: SourceReader,
             import_mode: str = ImportMode.UPSERT,
             progress: ProgressReporter | None = None) -> MasterValidation:
    """Read, clean and validate a master file without writing anything."""
    progress = progress or ProgressReporter(None)
    # Chosen before the file is opened, so the reader does not pick the table by
    # format instead. A master upload's phases run in a different order from the
    # ETL's — the codes are loaded before the rows are checked — and read against
    # the transactional table the whole validation pass fell inside a band the
    # bar had already passed, leaving it motionless on the longest step.
    progress.use_scale(MASTER_SCALE)
    result = MasterValidation(upload_type=upload_type)
    result.headers = [str(h) for h in reader.headers if h is not None]

    header_map = _header_index(upload_type, result.headers)
    mapped_targets = set(header_map.values())
    result.unmapped_columns = [h for h in result.headers if h not in header_map]

    by_target = upload_type.column_by_target
    result.missing_columns = [
        by_target[target].name
        for target in (c.target for c in upload_type.columns if c.required)
        if target not in mapped_targets
    ]
    if result.missing_columns:
        result.fatal = (
            "Required column(s) missing from the file: "
            + ", ".join(result.missing_columns)
            + ". Nothing was loaded. Download the template and use its header row."
        )
        result.issues.append(UploadIssue(
            row_number=1, column=", ".join(result.missing_columns), value=None,
            error_code=code.MISSING_COLUMN,
            message=result.fatal,
            suggested_fix="Download the template for this upload type and copy its "
                          "header row into your file.",
        ))
        progress.finish(message=result.fatal, failed=True)
        return result

    key_columns = upload_type.business_key
    key_label = ", ".join(by_target[c].name for c in key_columns if c in by_target)

    # The master codes this file will be checked against, loaded once for the
    # whole file rather than per row: a customer file naming the same
    # sub-territory ten thousand times must not be ten thousand queries. This is
    # the step the operator sees as "mapping with master data", so it is reported
    # as such — it is also the one that is slow on a cold cache.
    progress.phase(Phase.MAPPING)
    existing_codes = _existing_codes(session, upload_type)
    parent_codes = _parent_codes(session, upload_type)
    lookup_codes = _lookup_codes(session, upload_type)
    extra_check = ROW_CHECKS.get(upload_type.key)
    seen: dict[tuple, int] = {}

    # Materialised before the loop so the progress bar has a real denominator.
    # A master file describes one dimension and is orders of magnitude smaller
    # than a transaction file, so holding it is not the cost it would be there.
    #
    # The reader announces READING itself, as it parses — which is where the
    # wait actually is. By the time this runs the rows are already in hand, so
    # announcing the phase again here would only reset its counts to zero for a
    # pass that has finished.
    source_rows = list(reader)
    progress.phase(Phase.VALIDATING, total=len(source_rows))
    valid_so_far = 0
    invalid_so_far = 0

    for position, source_row in enumerate(source_rows, start=1):
        row = MasterRow(row_number=source_row.row_number,
                        raw={str(k): v for k, v in source_row.values.items()})
        _clean_row(row, upload_type, header_map, source_row)
        _check_required(row, upload_type)
        _check_parent(row, upload_type, parent_codes)
        _check_lookups(row, upload_type, lookup_codes)
        if extra_check is not None:
            extra_check(session, upload_type, row)

        key = _row_key(row, key_columns)
        if key is not None:
            if key in seen:
                row.action = "DUPLICATE"
                row.issues.append(UploadIssue(
                    row_number=row.row_number, column=key_label,
                    value=" / ".join(str(part) for part in key),
                    error_code=code.DUPLICATE_IN_FILE,
                    message=(f"'{' / '.join(str(p) for p in key)}' already appears "
                             f"at row {seen[key]} of this file."),
                    suggested_fix="Keep one row per record and delete the repeat.",
                ))
            else:
                seen[key] = row.row_number
                row.action = _decide_action(key in existing_codes, import_mode, row,
                                            key_label, key)
        result.rows.append(row)
        # Counted as we go rather than read back from the ``valid_rows`` and
        # ``invalid_row_numbers`` properties, which each walk the whole list and
        # would turn this loop quadratic when sampled.
        if row.issues:
            invalid_so_far += 1
        elif row.action != "DUPLICATE":
            valid_so_far += 1
        if progress.every(position, len(source_rows)):
            progress.rows(position, valid=valid_so_far, invalid=invalid_so_far)

    progress.counts(total_records=result.total_rows,
                    processed_records=result.total_rows,
                    valid_records=len(result.valid_rows),
                    invalid_records=len(result.invalid_row_numbers))
    return result


def _row_key(row: MasterRow, key_columns: tuple[str, ...]) -> tuple | None:
    """The row's identity, as a tuple. ``None`` when any part is missing.

    A tuple rather than a scalar because a record is not always identified by
    one column — map locations are keyed on entity type *and* code — and a
    one-column key is just the one-element case of the same rule.
    """
    parts = []
    for column in key_columns:
        value = row.values.get(column)
        if value is None:
            return None
        parts.append(value)
    return tuple(parts)


def _decide_action(exists: bool, import_mode: str, row: MasterRow,
                   key_column: str, key: tuple | str) -> str:
    """Reconcile the record's existence with the requested import mode."""
    shown = " / ".join(str(part) for part in key) if isinstance(key, tuple) else str(key)
    if exists and import_mode == ImportMode.INSERT:
        row.issues.append(UploadIssue(
            row_number=row.row_number, column=key_column, value=shown,
            error_code=code.ALREADY_EXISTS,
            message=f"'{shown}' already exists and the import mode is Insert only.",
            suggested_fix="Use 'Insert or update' to refresh the existing record, "
                          "or remove this row.",
        ))
        return "SKIP"
    if not exists and import_mode == ImportMode.UPDATE:
        row.issues.append(UploadIssue(
            row_number=row.row_number, column=key_column, value=shown,
            error_code=code.NOT_FOUND,
            message=f"'{shown}' does not exist and the import mode is Update only.",
            suggested_fix="Use 'Insert or update' to create it, or remove this row.",
        ))
        return "SKIP"
    return "UPDATE" if exists else "INSERT"


def _clean_row(row: MasterRow, upload_type: UploadType,
               header_map: dict[str, str], source_row: SourceRow) -> None:
    by_target = upload_type.column_by_target
    for header, target in header_map.items():
        column = by_target[target]
        value, error = clean_value(column, source_row.values.get(header))
        if error:
            row.issues.append(UploadIssue(
                row_number=row.row_number, column=column.name,
                value=_as_text(source_row.values.get(header)),
                error_code=code.INVALID_TYPE,
                message=f"{column.name}: {error}.",
                suggested_fix=column.guidance,
            ))
            continue
        row.values[target] = value


def _check_required(row: MasterRow, upload_type: UploadType) -> None:
    reported = {issue.column for issue in row.issues}
    for column in upload_type.columns:
        if not column.required:
            continue
        if column.name in reported:
            continue        # already rejected for a type failure; one reason each
        if is_blank(row.values.get(column.target)):
            row.issues.append(UploadIssue(
                row_number=row.row_number, column=column.name, value=None,
                error_code=code.MISSING_REQUIRED,
                message=f"{column.name} is required but is empty.",
                suggested_fix=f"Enter a value. {column.guidance}",
            ))


def _check_parent(row: MasterRow, upload_type: UploadType,
                  parent_codes: set[str] | None) -> None:
    """A child dimension row must name a parent that already exists."""
    if parent_codes is None or upload_type.parent_column is None:
        return
    value = row.values.get(upload_type.parent_column)
    if is_blank(value):
        return              # already reported by the required-field check
    if value in parent_codes:
        return
    column = upload_type.column_by_target.get(upload_type.parent_column)
    row.issues.append(UploadIssue(
        row_number=row.row_number,
        column=column.name if column else upload_type.parent_column,
        value=str(value),
        error_code=code.INVALID_PARENT,
        message=(
            f"{upload_type.parent_column.replace('_', ' ')} '{value}' does not exist "
            f"in {upload_type.parent_table}."
        ),
        suggested_fix=(
            f"Load {upload_type.parent_table} first, or correct the code to one that "
            "already exists."
        ),
    ))


def _check_lookups(row: MasterRow, upload_type: UploadType,
                   lookup_codes: dict[str, set[str]]) -> None:
    """A referencing column must name a record that exists.

    Blank is allowed — these references are optional, and a customer whose
    sub-territory is not yet known is a legitimate row, reported later as
    unmapped rather than refused now. What is refused is a code that is filled
    in and wrong, because that is a mistake the person can fix.
    """
    for lookup in upload_type.lookups:
        value = row.values.get(lookup.column)
        if is_blank(value):
            continue
        known = lookup_codes.get(lookup.column)
        if known is None or str(value) in known:
            continue
        column = upload_type.column_by_target.get(lookup.column)
        row.issues.append(UploadIssue(
            row_number=row.row_number,
            column=column.name if column else lookup.column,
            value=str(value),
            error_code=code.INVALID_PARENT,
            message=f"Invalid {lookup.label}: {value}",
            suggested_fix=(
                f"Use a {lookup.label.lower()} that already exists in "
                f"{lookup.table}, or leave the column empty to load the record "
                "unmapped."
            ),
        ))


def _lookup_codes(session: Session,
                  upload_type: UploadType) -> dict[str, set[str]]:
    """Every valid value for each of this type's lookup columns."""
    from .registry import MASTER_MODEL_BY_TABLE as _models

    codes: dict[str, set[str]] = {}
    for lookup in upload_type.lookups:
        model = _models.get(lookup.table)
        if model is None:
            continue
        column = getattr(model, lookup.target_column, None)
        if column is None:
            continue
        codes[lookup.column] = {
            value for (value,) in session.execute(select(column)).all() if value
        }
    return codes


def _existing_codes(session: Session, upload_type: UploadType) -> set[tuple]:
    """Every record already present, as business-key tuples."""
    model = MASTER_MODEL_BY_TABLE[upload_type.table]
    columns = [getattr(model, name) for name in upload_type.business_key]
    return {tuple(row) for row in session.execute(select(*columns)).all()}


def _parent_codes(session: Session, upload_type: UploadType) -> set[str] | None:
    if not upload_type.parent_table or not upload_type.parent_column:
        return None
    parent_model = MASTER_MODEL_BY_TABLE.get(upload_type.parent_table)
    if parent_model is None:  # pragma: no cover - registry keeps these in step
        return None
    column = getattr(parent_model, upload_type.parent_column)
    return {value for (value,) in session.execute(select(column)).all()}


@dataclass
class MasterLoadResult:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


def load(session: Session, upload_type: UploadType,
         validation: MasterValidation,
         progress: ProgressReporter | None = None) -> MasterLoadResult:
    """Apply the valid rows of a validated file. Caller owns the transaction.

    Only rows that passed every check are applied. An invalid row is never
    partially written, and a valid row is never held back because a different
    row failed — the batch is reported as PARTIAL instead.
    """
    progress = progress or ProgressReporter(None)
    model = MASTER_MODEL_BY_TABLE[upload_type.table]
    key_columns = upload_type.business_key
    columns = {c.key for c in model.__table__.columns}
    result = MasterLoadResult()

    applicable = validation.valid_rows
    progress.phase(Phase.IMPORTING, total=len(applicable))

    # Every record this dimension already holds, keyed the way the file keys its
    # rows, read in one statement.
    #
    # This was a SELECT per row. On SQLite that is invisible — 2,000 customers
    # cost 4,007 statements and under a second — but every one of them is a
    # network round trip to a pooled Postgres, where the same upload is tens of
    # seconds of waiting for answers the database could have given once. A master
    # dimension is bounded by the business rather than by the file (the largest
    # here is 2,091 customers), which is the same reason ``etl.mapping`` loads its
    # whole ``MasterDataIndex`` up front rather than resolving row by row.
    by_key: dict[tuple, Any] = {
        tuple(getattr(record, column) for column in key_columns): record
        for record in session.execute(select(model)).scalars()
    }
    #: New records, collected and written together at the end. See below.
    new_rows: list[dict[str, Any]] = []

    for position, row in enumerate(applicable, start=1):
        if progress.every(position, len(applicable)):
            progress.rows(position)
            progress.counts(imported_records=result.inserted + result.updated)
        if row.action == "SKIP":
            continue
        payload = {
            target: value for target, value in row.values.items()
            if target in columns
        }
        key = _row_key(row, key_columns)
        if key is None:
            continue

        # A composite-key dimension stores its key joined, so the fact table can
        # reference one column and an upsert can conflict on one. Derived here
        # rather than asked for in the file: it is not data the business has, it
        # is the codes the file already carries, written once — in the order
        # ``_COMPOSITE_KEYS`` declares, which is the order each key function
        # expects.
        for column, build in COMPOSITE_KEY_BUILDERS.items():
            if column in columns:
                payload[column] = build(*key)

        existing = by_key.get(key)

        if existing is None:
            # Held back rather than added to the session one at a time. The ORM
            # emits a separate statement per pending object even when they are
            # flushed together — measured, not assumed: 2,000 new customers were
            # 2,000 dispatches to the driver either way — while the same rows
            # through ``bulk_insert`` are one. On SQLite that is a fifth of a
            # second either way; against a pooled Postgres it is 2,000 network
            # round trips against one.
            #
            # Nothing downstream needs the ORM object: the map below is only
            # read when a later row repeats a key, and ``valid_rows`` has already
            # dropped any repeat as DUPLICATE, so within one file a key is
            # applied at most once.
            new_rows.append(payload)
            result.inserted += 1
            continue

        changed = False
        for column, value in payload.items():
            if column in key_columns:
                continue
            # An UPDATE must not blank a column the file simply omitted.
            if value is None and column not in row.values:
                continue
            if getattr(existing, column) != value:
                setattr(existing, column, value)
                changed = True
        # A placeholder created by the ETL becomes a real record once its master
        # arrives, so the flag is cleared rather than left misleadingly set.
        if "is_placeholder" in columns and getattr(existing, "is_placeholder", False):
            existing.is_placeholder = False
            changed = True
        # Re-uploading a retired code brings it back. Anything else would be a
        # trap: the loader would report the row as updated while the record
        # stayed invisible in the master-data tables.
        if "is_deleted" in columns and getattr(existing, "is_deleted", False):
            existing.is_deleted = False
            existing.deleted_at = None
            existing.deleted_by = None
            changed = True
        if changed:
            result.updated += 1
        else:
            result.unchanged += 1

    # One flush and one insert for the file, where there was a flush and a
    # statement per row. Both have to happen before the post-load work below:
    # ``_promote_source_status`` counts the dimension's rows, and a count taken
    # with these still pending would miss exactly the records just loaded.
    #
    # ``bulk_insert`` is the loader that the fact tables already go through, so
    # the row alignment and the chunking against the dialect's bind-parameter
    # ceiling are the ones this project has always used rather than a second
    # set written here.
    session.flush()
    if new_rows:
        bulk_insert(session, model.__table__, new_rows)

    if result.inserted or result.updated:
        _promote_source_status(session, upload_type)
        after_load = POST_LOAD.get(upload_type.key)
        if after_load is not None:
            after_load(session, upload_type, result)
    progress.counts(imported_records=result.inserted + result.updated)
    return result


# ---------------------------------------------------------------------------
# Upload-type specific rules
# ---------------------------------------------------------------------------


def _check_map_location(session: Session, upload_type: UploadType,
                        row: MasterRow) -> None:
    """Coordinates need checks the generic loader cannot know about.

    Three of them: the entity type must be one the map understands, the code
    must exist in the master data (a coordinate for a territory that does not
    exist would silently never draw), and the latitude/longitude must be a real
    place — which is where a spreadsheet's ``0`` gets caught.
    """
    from ..map.entities import ENTITY_TYPE_BY_KEY
    from ..map.geo import InvalidCoordinate, validate as validate_coordinate

    entity_type = row.values.get("entity_type")
    if entity_type and entity_type not in ENTITY_TYPE_BY_KEY:
        row.issues.append(UploadIssue(
            row_number=row.row_number, column="Entity Type", value=str(entity_type),
            error_code=code.INVALID_TYPE,
            message=f"'{entity_type}' is not a map entity type.",
            suggested_fix="Use one of: "
                          + ", ".join(sorted(ENTITY_TYPE_BY_KEY)),
        ))
        entity_type = None

    entity_code = row.values.get("entity_code")
    if entity_type and entity_code and not _entity_exists(session, entity_type,
                                                          entity_code):
        row.issues.append(UploadIssue(
            row_number=row.row_number, column="Entity Code", value=str(entity_code),
            error_code=code.INVALID_PARENT,
            message=(f"{entity_type} '{entity_code}' does not exist in the master "
                     "data."),
            suggested_fix="Load the master data for this entity first, or correct "
                          "the code.",
        ))

    latitude = row.values.get("latitude")
    longitude = row.values.get("longitude")
    if latitude is not None and longitude is not None:
        try:
            validate_coordinate(float(latitude), float(longitude))
        except (InvalidCoordinate, TypeError, ValueError) as exc:
            row.issues.append(UploadIssue(
                row_number=row.row_number, column="Latitude / Longitude",
                value=f"{latitude}, {longitude}",
                error_code=code.INVALID_TYPE, message=str(exc),
                suggested_fix="Enter decimal degrees, e.g. 23.7808 and 90.4008.",
            ))


def _entity_exists(session: Session, entity_type: str, entity_code: str) -> bool:
    from sqlalchemy import func as sa_func

    from ..map.entities import ENTITY_TYPE_BY_KEY

    entity = ENTITY_TYPE_BY_KEY[entity_type]
    model = MASTER_MODEL_BY_TABLE.get(entity.table)
    if model is None:
        return True                      # nothing to check against yet
    column = getattr(model, entity.code_field, None)
    if column is None:
        return True
    return bool(session.execute(
        select(sa_func.count()).select_from(model.__table__)
        .where(column == entity_code)
    ).scalar_one())


def _after_map_locations(session: Session, upload_type: UploadType,
                         result: "MasterLoadResult") -> None:
    """Re-derive parent centroids and drop the marker cache after a geo load."""
    from ..map.geo import derive_parents
    from ..map.resolver import invalidate_cache

    derive_parents(session)
    invalidate_cache()


#: Extra per-row validation, by upload type. Absent means the generic rules are
#: the whole story.
ROW_CHECKS = {"map_entity_locations": _check_map_location}

#: What to do after a successful load, by upload type.
POST_LOAD = {"map_entity_locations": _after_map_locations}


def _promote_source_status(session: Session, upload_type: UploadType) -> None:
    """Flip a PENDING_SOURCE_DATA dimension to AVAILABLE once it has records.

    This is the switch that turns a tolerated unknown customer code into a
    rejection: while no customer master existed, the ETL kept the code on the
    fact row; once one does, an unknown code is a data error.
    """
    if not upload_type.pending_source:
        return
    model = MASTER_MODEL_BY_TABLE[upload_type.table]
    count = session.execute(
        select(func.count()).select_from(model.__table__)
    ).scalar_one()
    if not count:
        return
    status = session.get(MasterSourceStatus, upload_type.table)
    if status is None:
        session.add(MasterSourceStatus(
            table_name=upload_type.table, status=STATUS_AVAILABLE,
            source_description="Uploaded through the Data Upload Center.",
            note="Populated by user upload; unknown codes are now rejected.",
        ))
    else:
        status.status = STATUS_AVAILABLE
        status.source_description = "Uploaded through the Data Upload Center."
        status.note = "Populated by user upload; unknown codes are now rejected."


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)[:500]


__all__ = [
    "MasterRow",
    "MasterValidation",
    "MasterLoadResult",
    "validate",
    "load",
    "clean_value",
]
