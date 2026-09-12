"""The ETL pipeline: source -> staging -> validation -> mapping -> facts.

One public entry point, :func:`run_import`, performs the thirteen steps of the
Phase 2 process for any data type and any source:

 1. accept a source (file or reader)      8. validate dates
 2. create an import batch                9. validate numerics
 3. detect the source structure          10. detect duplicates
 4. load staging                         11. load valid rows into the fact table
 5. validate required fields             12. reject invalid rows with a reason
 6. validate master codes                13. write the import summary
 7. validate the hierarchy

Nothing is loaded that has not passed every check, and nothing that fails is
dropped: it lands in ``etl_rejected_records`` with a code, a message and the
original row.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import Engine, func, literal, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database.models_warehouse import (
    BATCH_COMPLETED,
    BATCH_COMPLETED_WITH_ERRORS,
    BATCH_FAILED,
    BATCH_STARTED,
    BATCH_VALIDATING,
    FACT_MODEL_BY_DATA_TYPE,
    STAGING_DUPLICATE,
    STAGING_MODEL_BY_DATA_TYPE,
    STAGING_REJECTED,
    STAGING_VALID,
    EtlImportBatch,
    EtlRejectedRecord,
)
from ..utils.progress import Phase, ProgressReporter
from ..utils.text import normalize_text
from . import credit as credit_rules
from . import errors
from .bulk import bulk_insert, bulk_upsert, chunked, existing_keys, parameter_limit
from .calendar import ensure_dates_exist, to_date_id
from .datasets import DatasetSpec, FieldKind, get_dataset, map_headers
from .errors import ErrorSpec
from .mapping import MasterDataIndex, ensure_master_source_status
from .readers import SourceReader, SourceRow, reader_for_file
from .period import RECORD_PERIOD_RESOLVERS
from .transforms import (
    DERIVATIONS,
    LOAD_NOTES,
    LoadOptions,
    MEASURE_BUILDERS,
    PASSTHROUGH_COLUMNS,
    apply_volume,
)
from . import volume as volume_mod
from .volume import VolumeResult
from .validation import DATE_FORMAT_AUTO, parse_date, parse_number

LOAD_MODE_INITIAL = "INITIAL"
LOAD_MODE_INCREMENTAL = "INCREMENTAL"
LOAD_MODE_REPROCESS = "REPROCESS"

#: Identity and measure fields a line-level report names, in display order.
#: Only those the dataset actually declares are reported, so a target file is
#: described by its month and territory rather than by empty sales columns.
LINE_REPORT_FIELDS: tuple[str, ...] = (
    "company_code", "invoice_no", "invoice_line_no",
    "target_month", "territory_code",
    "customer_code", "material_code", "batch_code", "quantity",
)

#: How many per-line outcomes one import keeps in memory.
#:
#: The preview shows the first fifty rows and a file may hold a million, so
#: retaining an outcome for every row would cost far more than it is worth. The
#: *persisted* line report — built from staging after a real import — has no
#: such limit; this bounds only what a dry run can hand back without a database
#: to read from.
LINE_REPORT_LIMIT = 500

ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"


@dataclass
class Rejection:
    """One rejected row, ready to be written to ``etl_rejected_records``."""

    row_number: int
    error: ErrorSpec
    message: str
    raw_data: dict[str, Any]
    field_name: str | None = None
    field_value: str | None = None


@dataclass
class LineOutcome:
    """What happened to one source line, in the terms the operator uses.

    The upload preview and the duplicate report both need the same thing: the
    row number, the codes that decided the line's identity, the total volume it
    supplied, and whether it was taken and why not. Built once here so the two
    cannot drift apart.
    """

    row_number: int
    status: str
    reason: str | None
    fields: dict[str, Any]
    volume: Any = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "row": self.row_number,
            **{name: _as_text(value) for name, value in self.fields.items()},
            "volume": None if self.volume is None else str(self.volume),
            "status": self.status,
            "error_code": self.error_code,
            "reason": self.reason or ("Valid" if self.status == ACCEPTED else None),
        }


@dataclass
class Restatement:
    """What a scoped restatement voided, and what it was bounded by.

    Carried on the result so a **dry run can report it before anything is
    committed**, which is the whole point: a person has to be able to see what a
    load is about to stand down before it stands it down.
    """

    #: The column the scope is expressed in, and the values declared for it.
    scope_field: str
    scope: tuple[str, ...]
    #: Rows inside the scope that this file did not restate, and their balance.
    #: These are what the load voids.
    voided_rows: int = 0
    voided_amount: Decimal = Decimal("0")
    #: Of those, the ones this very file *rejected*. They are the sharpest edge
    #: of a restatement: the file names the invoice, the loader refuses the row,
    #: and the warehouse row is stood down with nothing put in its place. Counted
    #: separately so the preview can say so in those words.
    voided_because_rejected: int = 0
    voided_because_rejected_amount: Decimal = Decimal("0")
    #: Identities inside the scope that the file still names.
    restated_rows: int = 0
    #: Rows voided because a **newer row for the same invoice** replaced them,
    #: rather than because the file stopped naming them.
    #:
    #: They exist because a business key names the fields it was built from and
    #: the source system that supplied them, so the same invoice arriving from a
    #: second extract cannot be updated in place — it is written beside the old
    #: row and the old row is stood down. Counted apart from a settlement
    #: because they are the opposite of one: the debt did not go away, its
    #: authoritative record moved.
    superseded_rows: int = 0
    superseded_amount: Decimal = Decimal("0")
    #: Rows the load wrote that were outside any existing scope row — new
    #: business, not a restatement of anything.
    new_rows: int = 0
    #: What the upload declared it was **holding out** of its own claim, and the
    #: rows that were spared as a result.
    #:
    #: A scope says "this file states the whole of X". A hold says "…except for
    #: these, whose absence from the file I do not accept as settlement" — which
    #: is a narrower claim, never a wider one, and the only direction a
    #: declaration is allowed to move a void in.
    held_field: str | None = None
    held: tuple[str, ...] = ()
    held_rows: int = 0
    held_amount: Decimal = Decimal("0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_field": self.scope_field,
            "scope": list(self.scope),
            "voided_rows": self.voided_rows,
            "voided_amount": float(self.voided_amount),
            "voided_because_rejected": self.voided_because_rejected,
            "voided_because_rejected_amount":
                float(self.voided_because_rejected_amount),
            "restated_rows": self.restated_rows,
            "superseded_rows": self.superseded_rows,
            "superseded_amount": float(self.superseded_amount),
            "new_rows": self.new_rows,
            "held_field": self.held_field,
            "held": list(self.held),
            "held_rows": self.held_rows,
            "held_amount": float(self.held_amount),
        }


@dataclass
class ImportResult:
    """Outcome of one import run."""

    batch_id: int | None
    batch_uuid: str
    data_type: str
    source_type: str
    source_system: str
    source_file: str | None
    load_mode: str
    status: str = BATCH_STARTED
    total_rows: int = 0
    valid_rows: int = 0
    rejected_rows: int = 0
    duplicate_rows: int = 0
    inserted_rows: int = 0
    updated_rows: int = 0
    unmapped_columns: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    error_counts: dict[str, int] = field(default_factory=dict)
    category_counts: dict[str, int] = field(default_factory=dict)
    #: Rows loaded without a total volume, by reason. Not rejections — the sale
    #: is valid and its net sales figure is real, the file simply stated no
    #: volume — so they are reported here for correction at source rather than
    #: counted against the batch.
    volume_gaps: dict[str, int] = field(default_factory=dict)
    #: Rows whose hierarchy was derived from the Customer Master.
    hierarchy_from_customer: int = 0
    #: Sentences a dataset wants said about the load as a whole, rather than
    #: about any one row.
    #:
    #: Credit Control is what this exists for: the SPL extract states its own
    #: overdue split in ``od`` and ``maturity``, both frozen at the date the
    #: extract was taken. Neither reaches a fact column — a stored overdue figure
    #: is wrong the next morning while still looking authoritative — but a load
    #: whose own derivation disagrees with the source's is a defect worth
    #: hearing about at the moment it happens, rather than a fortnight later when
    #: somebody queries a total. So the two are compared and the disagreement is
    #: stated here as a percentage.
    notes: list[str] = field(default_factory=list)
    #: How this file signs a deduction, as declared for this load.
    #:
    #: Recorded rather than merely used: the staging rows and every rejected
    #: row's ``raw_data`` keep the file's original signs, so without knowing
    #: which convention they were read under nobody could interpret them again
    #: afterwards. The fact rows themselves are canonical and need no such key.
    deduction_convention: str | None = None
    #: What the scoped restatement did, or ``None`` when this load was not one.
    restatement: "Restatement | None" = None
    #: Per-line verdicts for the first :data:`LINE_REPORT_LIMIT` rows, which is
    #: what the upload preview shows. Like ``rejections``, carried on the result
    #: so a dry run can report them after rolling everything back.
    lines: list["LineOutcome"] = field(default_factory=list)
    message: str | None = None
    #: Every rejected row, with its reason. Carried on the result rather than
    #: only in ``etl_rejected_records`` so that a ``dry_run`` — which rolls the
    #: whole transaction back — can still report *which row* failed and why.
    #: Deliberately excluded from :meth:`to_dict`: the JSON summary stays a
    #: summary, and raw source rows are never returned by the import API.
    rejections: list["Rejection"] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "batch_uuid": self.batch_uuid,
            "data_type": self.data_type,
            "source_type": self.source_type,
            "source_system": self.source_system,
            "source_file": self.source_file,
            "load_mode": self.load_mode,
            "status": self.status,
            "totals": {
                "total_rows": self.total_rows,
                "valid_rows": self.valid_rows,
                "rejected_rows": self.rejected_rows,
                "duplicate_rows": self.duplicate_rows,
                "inserted_rows": self.inserted_rows,
                "updated_rows": self.updated_rows,
            },
            "unmapped_columns": self.unmapped_columns,
            "missing_columns": self.missing_columns,
            "error_counts": self.error_counts,
            "category_counts": self.category_counts,
            "volume_gaps": self.volume_gaps,
            "hierarchy_from_customer": self.hierarchy_from_customer,
            "message": self.message,
        }

    def render(self) -> str:
        lines = [
            f"{self.data_type.title()} Import Summary",
            "=" * 60,
            f"Batch:        {self.batch_id} ({self.batch_uuid})",
            f"Source:       {self.source_file or self.source_type} [{self.source_system}]",
            f"Load mode:    {self.load_mode}",
            f"Status:       {self.status}",
            "",
            f"Total:        {self.total_rows}",
            f"Valid:        {self.valid_rows}",
            f"Rejected:     {self.rejected_rows}",
            f"  Inserted:   {self.inserted_rows}",
            f"  Updated:    {self.updated_rows}",
            f"  Duplicates: {self.duplicate_rows}",
        ]
        if self.missing_columns:
            lines += ["", "Missing required columns: " + ", ".join(self.missing_columns)]
        if self.unmapped_columns:
            lines += ["Unmapped source columns:  " + ", ".join(self.unmapped_columns)]
        if self.error_counts:
            lines += ["", "Rejection reasons:"]
            for code, count in sorted(self.error_counts.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {code}: {count}")
        if self.message:
            lines += ["", self.message]
        return "\n".join(lines)


class EtlPipeline:
    """Runs one import. Instantiate per file; state is not reused."""

    def __init__(
        self,
        session: Session,
        data_type: str,
        reader: SourceReader,
        *,
        source_system: str = "MANUAL",
        load_mode: str = LOAD_MODE_INCREMENTAL,
        date_format: str | None = None,
        master_index: MasterDataIndex | None = None,
        progress: ProgressReporter | None = None,
        deduction_convention: str | None = None,
        restatement_scope: Sequence[str] | None = None,
        restatement_hold: tuple[str, Sequence[str]] | None = None,
    ) -> None:
        self.session = session
        self.spec: DatasetSpec = get_dataset(data_type)
        self.reader = reader
        self.source_system = source_system
        self.load_mode = load_mode
        self.date_format = date_format or get_settings().default_date_format
        self._master_index = master_index
        #: Where the run reports itself to. A reporter with no job is a no-op, so
        #: the emission calls below stay unconditional and cost nothing for the
        #: scripted imports that nobody is watching.
        self.progress = progress or ProgressReporter(None)

        self.staging_model = STAGING_MODEL_BY_DATA_TYPE[self.spec.data_type]
        self.fact_model = FACT_MODEL_BY_DATA_TYPE[self.spec.data_type]
        self.batch: EtlImportBatch | None = None
        self.rejections: list[Rejection] = []
        #: ``row number -> volume`` for the rows that reached the fact builder,
        #: so the preview can show the total the file stated for each line.
        self._volume_by_row: dict[int, Any] = {}
        #: Why the supplied volume is unusable, by reason. A gap in the source
        #: file rather than a bad row, so it is counted and reported instead of
        #: rejecting a sale that is otherwise perfectly good.
        self.volume_gaps: dict[str, int] = {}
        #: Rows whose organisational hierarchy came from the Customer Master
        #: rather than from codes in the file. Reported, not hidden.
        self.hierarchy_from_customer = 0

        #: How this file signs a deduction. Required where the dataset says the
        #: question has an answer, and refused up front rather than defaulted:
        #: picking a side silently is the one failure this parameter exists to
        #: prevent, and it surfaces months later as a balance off by twice the
        #: payment. The caller gets the default from
        #: ``credit.detect_convention`` and a person confirms it in the preview.
        if self.spec.requires_deduction_convention:
            if deduction_convention not in credit_rules.DEDUCTION_CONVENTIONS:
                raise ValueError(
                    f"{self.spec.data_type} must be told how its file signs a "
                    f"deduction: expected one of "
                    f"{credit_rules.DEDUCTION_CONVENTIONS}, got "
                    f"{deduction_convention!r}. It is declared per upload "
                    "because two real extracts disagree, and inferring it "
                    "silently is what this refusal prevents."
                )
        elif deduction_convention is not None:
            raise ValueError(
                f"{self.spec.data_type} states no deductions, so a deduction "
                f"convention ({deduction_convention!r}) means nothing here. A "
                "setting that changes nothing is a setting somebody will trust."
            )
        self.deduction_convention = deduction_convention

        #: The values of ``spec.restatement_scope_field`` this load claims to
        #: restate in full. Empty means an ordinary incremental load: nothing is
        #: voided, which is what every dataset but receivables does.
        if restatement_scope and self.spec.restatement_scope_field is None:
            raise ValueError(
                f"{self.spec.data_type} cannot be restated: it declares no "
                "restatement scope field, so there is no column to bound the "
                "claim with and nothing may be voided."
            )
        self.restatement_scope: tuple[str, ...] = tuple(restatement_scope or ())

        #: ``(field, values)`` this load declares it is **not** restating, even
        #: though they fall inside its scope. Their rows are excluded from the
        #: snapshot entirely, so they can never be voided by this load.
        #:
        #: The case it exists for is real and was found on the first deployment:
        #: a receivables extract for one company omitted an entire customer's
        #: thirteen invoices. Read as a restatement that is thirteen settlements
        #: worth ৳2.81 Cr; read as a person who knows the business would read it,
        #: it is an extract that is missing a customer. The scope is expressed
        #: per company, so it cannot express that doubt — and widening the scope
        #: is not the answer, because the doubt is *narrower* than the scope, not
        #: broader.
        #:
        #: It is a declaration like the scope itself, and it only ever spares
        #: rows. A hold cannot cause anything to be voided that would otherwise
        #: have survived, which is what makes it safe to accept from a caller.
        self.restatement_hold: tuple[str, tuple[str, ...]] | None = None
        if restatement_hold is not None:
            field_name, values = restatement_hold
            if self.spec.restatement_scope_field is None:
                raise ValueError(
                    f"{self.spec.data_type} cannot be restated, so there is "
                    "nothing for a hold to narrow."
                )
            if field_name not in self.fact_model.__table__.columns:
                raise ValueError(
                    f"'{field_name}' is not a column of "
                    f"{self.spec.fact_table}, so rows cannot be held out by it."
                )
            self.restatement_hold = (field_name, tuple(values))
        #: Identities of rows this file named and the loader refused. Used only
        #: by the restatement, to say how much of what it stands down is
        #: standing down because of a defect rather than a settlement.
        self._rejected_keys: set[tuple] = set()
        #: Identity -> the business keys the warehouse holds it under. One
        #: identity can have several, which is what `…#2` numbering produces.
        self._keys_by_identity: dict[tuple, list[str]] = {}

    # -- steps --------------------------------------------------------------

    def _create_batch(self) -> EtlImportBatch:
        """Step 2: open the audit record before anything is read."""
        batch = EtlImportBatch(
            batch_uuid=str(uuid.uuid4()),
            source_type=self.reader.source_type,
            source_system=self.source_system,
            source_file=self.reader.source_name,
            data_type=self.spec.data_type,
            load_mode=self.load_mode,
            status=BATCH_STARTED,
            deduction_convention=self.deduction_convention,
            restatement_scope=list(self.restatement_scope) or None,
        )
        self.session.add(batch)
        self.session.flush()
        self.batch = batch
        return batch

    def _detect_structure(self) -> tuple[dict[str, str], list[str], list[str]]:
        """Step 3: map source headers onto canonical fields."""
        headers = self.reader.headers
        mapping, unmapped = map_headers(self.spec, headers)
        mapped_fields = set(mapping.values())
        missing = [f for f in self.spec.required_fields if f not in mapped_fields]
        return mapping, unmapped, missing

    def _to_canonical(self, row: SourceRow, header_map: dict[str, str]) -> dict[str, Any]:
        record: dict[str, Any] = {}
        for header, value in row.values.items():
            canonical = header_map.get(str(header))
            if canonical is not None:
                record[canonical] = value
        return record

    def _load_staging(self, rows: list[tuple[SourceRow, dict[str, Any]]]) -> None:
        """Step 4: persist every source row exactly as it arrived."""
        assert self.batch is not None
        staging_columns = set(self.staging_model.__table__.columns.keys())
        payload = []
        for source_row, record in rows:
            staged = {
                key: _as_text(value)
                for key, value in record.items() if key in staging_columns
            }
            staged.update({
                "import_batch_id": self.batch.batch_id,
                "source_file": self.reader.source_name,
                "source_row_number": source_row.row_number,
                "source_system": self.source_system,
                "raw_data": {str(k): _as_text(v) for k, v in source_row.values.items()},
                "validation_status": "PENDING",
            })
            payload.append(staged)
        bulk_insert(self.session, self.staging_model.__table__, payload,
                    on_chunk=self.progress.rows)

    def _clean_record(self, record: dict[str, Any], row: SourceRow) -> dict[str, Any] | None:
        """Steps 5, 8, 9: required fields, dates and numerics."""
        cleaned: dict[str, Any] = {}
        failed = False
        #: Fields already rejected for a parse failure. They must not *also* be
        #: reported as "required field empty" — one defect, one reason.
        failed_fields: set[str] = set()

        for spec in self.spec.fields:
            raw = record.get(spec.name)

            if spec.kind == FieldKind.DATE:
                result = parse_date(raw, self.date_format)
                if not result.ok:
                    self._reject(row, result.error, result.message or "", spec.name, raw)
                    failed = True
                    failed_fields.add(spec.name)
                    continue
                cleaned[spec.name] = result.value

            elif spec.kind == FieldKind.NUMERIC:
                result = parse_number(raw, spec.allow_negative, spec.name)
                if not result.ok:
                    self._reject(row, result.error, result.message or "", spec.name, raw)
                    failed = True
                    failed_fields.add(spec.name)
                    continue
                value = result.value
                if value is None and spec.default_numeric is not None:
                    from decimal import Decimal
                    value = Decimal(str(spec.default_numeric))
                cleaned[spec.name] = value

            else:
                text = normalize_text(raw)
                # A spreadsheet error marker is not a value this column can
                # hold. `#N/A` is what Excel leaves behind when a lookup finds
                # nothing, and stored as a code it becomes a territory named
                # "#N/A" that groups, filters and totals like a real place — the
                # SPL extract carries it in every hierarchy column on 75 rows
                # worth ৳23.76 Cr, a fifth of that book.
                #
                # Rejected rather than blanked: blanking it would let the row
                # load with a hole where its hierarchy should be, and the money
                # would go missing from every organisational total without
                # appearing anywhere as a problem. The rejection keeps the whole
                # original row in `etl_rejected_records`.
                if text is not None and spec.reject_values and any(
                    text.strip().casefold() == bad.casefold()
                    for bad in spec.reject_values
                ):
                    self._reject(
                        row, errors.SPREADSHEET_ERROR_VALUE,
                        f"'{text}' is a spreadsheet error marker, not a "
                        f"{spec.name.replace('_', ' ')}. The row is kept in full "
                        "so the amount it carries stays visible.",
                        spec.name, raw,
                    )
                    failed = True
                    failed_fields.add(spec.name)
                    continue
                cleaned[spec.name] = text

        for name in self.spec.required_fields:
            if name in failed_fields:
                continue
            if cleaned.get(name) is None:
                self._reject(
                    row, errors.MISSING_REQUIRED_FIELD,
                    f"Required field '{name}' is empty.", name, record.get(name),
                )
                failed = True

        if failed:
            self._note_rejected_key(cleaned)
            return None
        resolved = self._resolve_period(cleaned, row)
        if resolved is None:
            return None
        return self._apply_derivations(resolved)

    def _note_rejected_key(self, cleaned: dict[str, Any]) -> None:
        """Remember which warehouse row a *rejected* row would have restated.

        Only meaningful during a restatement, and it is the sharpest edge of one.
        A file names an invoice, the loader refuses the row, and the warehouse
        row the file was restating is then stood down with nothing put in its
        place — the book falls by that amount for a reason that is a data-quality
        problem rather than a settlement.

        The SPL extract does exactly this 75 times, for ৳23.76 Cr of bank FDRs,
        share investments and inter-company loans carrying ``#N/A`` hierarchies.
        That has to be said in the preview, before the commit, in those words.

        A row whose *identity* fields failed to clean has no key to remember and
        is skipped: it names nothing, so it stands nothing down.
        """
        if self.spec.restatement_scope_field is None:
            return
        if any(cleaned.get(name) is None
               for name in self.spec.business_key_fields):
            return
        self._rejected_keys.add(self._identity(cleaned))

    def _identity(self, source) -> tuple:
        """What makes this row *the same invoice* as one already in the warehouse.

        The values of ``business_key_fields``, not the ``business_key`` string
        built from them — and the difference is the whole reason a restatement
        works at all across two extracts.

        That string names the fields it was built from **and the source system**,
        so a row loaded by one export can never share it with the same document
        loaded by another. The first deployment rehearsal is what proved it: the
        existing book was loaded as ``UPLOAD`` under the ``(company, invoice)``
        grain 0031 declared, the SPL extract arrives as ``SPL`` under
        ``(company, customer, invoice)``, and matching on the key string reported
        **zero** of 14,589 genuinely-matching invoices as restated and proposed
        voiding the entire company. Identity is what a person means by "the file
        still names this invoice"; the key string is an implementation detail of
        how one loader wrote it down.
        """
        return tuple(
            None if (value := source.get(name) if isinstance(source, dict)
                     else getattr(source, name, None)) is None else str(value)
            for name in self.spec.business_key_fields
        )

    def _scope_snapshot(self) -> dict[tuple, Decimal]:
        """Every open row inside the declared scope, with what it is worth.

        Taken **before** the write, and that ordering is the whole of why this is
        a separate step. Read afterwards, a row this load has just inserted is
        indistinguishable from one it restated — both are simply present — and
        the preview would report new business as though the file had confirmed
        something already on the books.
        """
        assert self.spec.restatement_scope_field is not None
        table = self.fact_model.__table__
        amount_column = (
            table.c[self.spec.restatement_amount_field]
            if self.spec.restatement_amount_field else None
        )
        columns = [table.c[name] for name in self.spec.business_key_fields]
        columns.append(table.c.business_key)
        if amount_column is not None:
            columns.append(amount_column)
        conditions = [
            table.c[self.spec.restatement_scope_field].in_(self.restatement_scope),
            table.c.is_void == False,  # noqa: E712 - portable on both dialects
        ]
        if self.restatement_hold is not None:
            field_name, values = self.restatement_hold
            if values:
                # Held out of the snapshot rather than filtered at the void, so
                # a held row is not merely spared — it is never a candidate, and
                # it does not appear in any count this load reports as dropped.
                conditions.append(table.c[field_name].notin_(values))
        rows = self.session.execute(select(*columns).where(*conditions)).all()
        width = len(self.spec.business_key_fields)
        snapshot: dict[tuple, Decimal] = {}
        self._keys_by_identity = {}
        for row in rows:
            identity = tuple(None if v is None else str(v) for v in row[:width])
            amount = (Decimal(str(row[width + 1] or 0))
                      if amount_column is not None else Decimal("0"))
            # A repeat of one identity is what the `…#2` numbering permits, so
            # both rows are held and both are voided or spared together — the
            # file names the invoice or it does not.
            snapshot[identity] = snapshot.get(identity, Decimal("0")) + amount
            self._keys_by_identity.setdefault(identity, []).append(row[width])
        return snapshot

    def _held_out(self) -> tuple[int, Decimal]:
        """How many live in-scope rows this load declined to consider, and their value.

        Counted and reported rather than left silent: a hold is the one thing
        that makes a restatement's void *smaller* than its scope implies, so a
        reader comparing the two needs the difference stated rather than having
        to infer it from a number that looks lower than expected.
        """
        if self.restatement_hold is None or not self.restatement_hold[1]:
            return 0, Decimal("0")
        assert self.spec.restatement_scope_field is not None
        table = self.fact_model.__table__
        field_name, values = self.restatement_hold
        amount_column = (
            table.c[self.spec.restatement_amount_field]
            if self.spec.restatement_amount_field else None
        )
        row = self.session.execute(
            select(
                func.count(),
                func.sum(amount_column) if amount_column is not None
                else literal(0),
            ).where(
                table.c[self.spec.restatement_scope_field].in_(self.restatement_scope),
                table.c[field_name].in_(values),
                table.c.is_void == False,  # noqa: E712
            )
        ).one()
        return int(row[0] or 0), Decimal(str(row[1] or 0))

    def _restate(self, before: dict[tuple, Decimal], written: set[tuple],
                 written_keys: set[str]) -> Restatement:
        """Void every in-scope row this file did not restate.

        **This is not REPLACE.** The distinction is the whole design and it has
        two halves. *Bounded*: only rows whose ``restatement_scope_field`` is one
        of the values declared for this upload are even looked at, so a
        receivables file for company 1000 cannot touch company 2000's book no
        matter what it contains or omits. *Voided, not deleted*: the row, its
        provenance and its batch survive, every reporting view filters
        ``is_void``, and a later file naming that key again updates the row back
        into life rather than inserting a second one.

        The precedent is ``targetmgmt.lock._void_dropped``, which restates a
        plan's own rows and voids the keys it drops — the same shape for the same
        reason, and scoped there to the plan's own batches exactly as this is
        scoped to declared values.

        The scope is a *declaration*, never a reading of the file. Inferring what
        to stand down from what happens to be present would mean a file that
        accidentally omitted a company erased that company's book, and the
        erasure would be indistinguishable from a correct restatement.
        """
        assert self.spec.restatement_scope_field is not None
        outcome = Restatement(
            scope_field=self.spec.restatement_scope_field,
            scope=self.restatement_scope,
        )
        if self.restatement_hold is not None:
            outcome.held_field, outcome.held = self.restatement_hold
            outcome.held_rows, outcome.held_amount = self._held_out()
        # Every in-scope row this load did not itself write is stood down, and
        # the reason is reported per row. Keyed on the business key rather than
        # on identity, because that is what says "this exact row is the one I
        # just wrote": an invoice the file restates arrives under a *new* key
        # when it comes from a different extract, so the old row is superseded
        # rather than updated, and leaving it live would double-count the debt.
        # On a re-upload of the same file the keys match, nothing is superseded,
        # and the upsert updates in place exactly as before.
        to_void: list[str] = []
        for identity, amount in before.items():
            keys = [key for key in self._keys_by_identity.get(identity, [])
                    if key not in written_keys]
            still_named = identity in written
            if still_named:
                outcome.restated_rows += 1
            if not keys:
                continue
            to_void.extend(keys)
            outcome.voided_rows += len(keys)
            outcome.voided_amount += amount
            if still_named:
                outcome.superseded_rows += len(keys)
                outcome.superseded_amount += amount
            elif identity in self._rejected_keys:
                outcome.voided_because_rejected += len(keys)
                outcome.voided_because_rejected_amount += amount
        outcome.new_rows = len(written - set(before))

        # Chunked against the dialect's bind-parameter ceiling: each key in the
        # IN clause is one parameter, and a restatement can stand down thousands
        # of rows at once. SQLite's ceiling is 32,766, which a full receivables
        # book would sail past on its second upload.
        now = datetime.now(timezone.utc)
        table = self.fact_model.__table__
        for chunk in chunked(to_void, parameter_limit(self.session)):
            self.session.execute(
                table.update()
                .where(table.c.business_key.in_(chunk))
                .values(is_void=True, voided_at=now)
            )
        return outcome


    def _apply_derivations(self, cleaned: dict[str, Any]) -> dict[str, Any]:
        """Add a dataset's derived fields to the cleaned record.

        Placed here, after cleaning and period resolution and before anything
        reads the record, because the derived values are inputs to three later
        steps rather than outputs of them: the business key may name them, the
        ``dim_date`` entries are created from them, and the fact builder maps
        them onto columns. Deriving inside the fact builder — the obvious place —
        would be after all three.

        A dataset with no derivation registered passes straight through, which is
        every dataset but credit invoices.
        """
        derive = DERIVATIONS.get(self.spec.data_type)
        if derive is None:
            return cleaned
        options = LoadOptions(deduction_convention=self.deduction_convention)
        return {**cleaned, **derive(cleaned, options)}

    def _resolve_period(self, cleaned: dict[str, Any],
                        row: SourceRow) -> dict[str, Any] | None:
        """Derive the date a dataset states as a period rather than a date.

        Targets are set for a month of a financial year and carry no date
        column, so the ``date_id`` every fact needs is resolved from that pair
        here — after the fields have been cleaned and before anything is mapped,
        which is the one point where both values are known and nothing has been
        keyed on them yet. Datasets with a real date column have no resolver
        registered and pass straight through.
        """
        resolver = RECORD_PERIOD_RESOLVERS.get(self.spec.data_type)
        if resolver is None:
            return cleaned
        outcome = resolver(cleaned)
        if not outcome.ok:
            assert outcome.error is not None
            self._reject(row, outcome.error, outcome.message or "",
                         outcome.field_name, outcome.field_value)
            return None
        cleaned.update(outcome.values)
        return cleaned

    def _map_master(self, cleaned: dict[str, Any], row: SourceRow,
                    index: MasterDataIndex) -> dict[str, Any] | None:
        """Steps 6 and 7: master-code existence and hierarchy consistency."""
        dimensions: dict[str, Any] = {}

        # A dataset that declares no organisational levels has no hierarchy to
        # resolve. Material stock is the case: it is located by plant and storage
        # location, and running the org resolver over it would reject every row
        # for carrying no region — a code its source has no reason to state.
        if self.spec.org_levels:
            org = index.resolve_org(cleaned, self.spec.org_levels)
            if not org.ok:
                for error in org.errors:
                    self._reject(row, error.error, error.message, error.field_name,
                                 error.field_value)
                return None
            if org.derived_from_customer:
                self.hierarchy_from_customer += 1
            dimensions.update(org.dimensions)

        if self.spec.material_required or self.spec.material_optional:
            material = index.resolve_material_code(
                cleaned.get("material_code"), self.spec.material_required
            )
            if not material.ok:
                for error in material.errors:
                    self._reject(row, error.error, error.message, error.field_name,
                                 error.field_value)
                return None
            dimensions.update(material.dimensions)

        fact_columns = set(self.fact_model.__table__.columns.keys())
        for table, dimension_field, code_field in (
            ("dim_customer", "customer_id", "customer_code"),
            ("dim_sales_force", "sales_force_id", "sales_force_code"),
        ):
            if dimension_field not in fact_columns:
                continue
            optional = index.resolve_optional(table, dimension_field, cleaned.get(code_field))
            if not optional.ok:
                for error in optional.errors:
                    self._reject(row, error.error, error.message, error.field_name,
                                 error.field_value)
                return None
            dimensions.update(optional.dimensions)

        # Material stock resolves against its own three masters instead of the
        # sales hierarchy. Driven off the fact table's own columns, like the
        # optional dimensions above, so no dataset needs a flag for it.
        #
        # Keyed on ``storage_location_id``, not ``material_id`` and not
        # ``plant_id``. Since revision 0022 every fact naming an item has a
        # ``material_id``, and the sales and target datasets resolved theirs
        # above — so that column does not distinguish stock. ``plant_id`` did
        # until ``fact_credit_invoice`` arrived, which names the plant that
        # raised an invoice and states no storage location or material at all;
        # keyed on the plant, every credit row would be rejected for missing
        # masters it has no reason to carry. The storage location is what
        # actually distinguishes a stock position: it is where the goods *are*,
        # and no other fact records one.
        if "storage_location_id" in fact_columns:
            stock = index.resolve_stock_masters(cleaned)
            if not stock.ok:
                for error in stock.errors:
                    self._reject(row, error.error, error.message, error.field_name,
                                 error.field_value)
                return None
            dimensions.update(stock.dimensions)

        # Existence and hierarchy are settled; what remains is whether the
        # customer and the sales force member actually belong where the row
        # files them. Only datasets that record an assignment ask for this.
        if self.spec.check_assignment_consistency:
            assignment = index.resolve_assignment(cleaned)
            if not assignment.ok:
                for error in assignment.errors:
                    self._reject(row, error.error, error.message, error.field_name,
                                 error.field_value)
                return None

        return dimensions

    def _describe_line(self, cleaned: dict[str, Any]) -> str:
        """Name the line the way the person reading the report thinks of it.

        The raw business key is a pipe-joined string with the field names
        prepended — precise, and unreadable. This says
        ``invoice INV001, material 1400000086, batch BATCH-A`` instead, naming
        exactly the fields that decided the line's identity so the reader can see
        *why* two rows collided.
        """
        labels = {
            "company_code": "company", "invoice_no": "invoice",
            "invoice_line_no": "line", "material_code": "material",
            "batch_code": "batch",
            "target_month": "month", "financial_year": "FY",
            "territory_code": "territory",
        }
        parts = []
        for name in self.spec.key_fields_for(cleaned):
            value = cleaned.get(name)
            if value in (None, ""):
                continue
            parts.append(f"{labels.get(name, name.replace('_', ' '))} {value}")
        return ", ".join(parts) or "this line"

    def _volume_for(self, cleaned: dict[str, Any],
                    row: SourceRow) -> VolumeResult | None:
        """This row's volume: the total the file supplied, and nothing else.

        Returns ``None`` when the row must be rejected, which happens for one
        reason only — a negative volume on a line whose quantity is not also
        negative. Everything else is counted and reported: a sale with no volume
        is still a sale, and rejecting it would discard a real net sales figure
        over a missing measurement.

        A dataset whose file carries no volume column has no volume. Nothing is
        inferred from the quantity and the Product Master's pack size: that
        inference is what made a warehouse figure depend on master data the
        source system never consulted, and it is gone from both sales and stock.
        """
        if "volume" not in self.spec.field_map:
            return None

        result = volume_mod.from_source(cleaned.get("volume"),
                                        cleaned.get("quantity"))

        if result.reason == volume_mod.NEGATIVE_VOLUME:
            self._reject(
                row, errors.VOLUME_NEGATIVE,
                "Negative volume on a line whose quantity is not negative: "
                f"{self._describe_line(cleaned)} supplied volume "
                f"{cleaned.get('volume')}. A return sends both negative.",
                field_name="volume", field_value=cleaned.get("volume"),
            )
            return None

        if result.reason:
            self.volume_gaps[result.reason] = (
                self.volume_gaps.get(result.reason, 0) + 1
            )
        return result

    def _build_fact_row(self, cleaned: dict[str, Any], dimensions: dict[str, Any],
                        row: SourceRow, business_key: str,
                        volume: VolumeResult | None = None) -> dict[str, Any]:
        """Assemble the fact row: dimensions + measures + provenance."""
        assert self.batch is not None
        fact_columns = set(self.fact_model.__table__.columns.keys())

        fact: dict[str, Any] = {
            key: value for key, value in dimensions.items() if key in fact_columns
        }
        # A dataset with no reporting date has no ``date_id`` — material stock is
        # a current position, and there is no date in the source to resolve one
        # from. Writing today's date here would turn "where stock stands" into a
        # dated observation the file never made.
        if self.spec.date_field is not None:
            fact["date_id"] = to_date_id(cleaned[self.spec.date_field])
        # A fact holding several dates names a column per date instead. Absent
        # dates write nothing rather than a default: an invoice with no clearing
        # date has genuinely not been cleared, and a placeholder would make an
        # open receivable look settled.
        for field_name, column in self.spec.date_columns:
            value = cleaned.get(field_name)
            if value is not None:
                fact[column] = to_date_id(value)
        measures = MEASURE_BUILDERS[self.spec.data_type](cleaned)

        # Volume, where the fact table has somewhere to put it: the total the
        # source supplied, stored as it stands. Nothing else contributes to it.
        if "volume" in fact_columns:
            if volume is None:
                volume = self._volume_for(cleaned, row)
            if volume is not None:
                measures = apply_volume(measures, volume)
                self._volume_by_row[row.row_number] = volume.volume
        fact.update(measures)

        for name in PASSTHROUGH_COLUMNS[self.spec.data_type]:
            if name in fact_columns:
                fact[name] = cleaned.get(name)

        fact.update({
            "source_system": self.source_system,
            "source_transaction_id": cleaned.get("source_transaction_id"),
            "source_file": self.reader.source_name,
            "source_row_number": row.row_number,
            "import_batch_id": self.batch.batch_id,
            "business_key": business_key,
        })
        return {key: value for key, value in fact.items() if key in fact_columns}

    def _line_outcomes(
        self,
        rows: list[tuple[SourceRow, dict[str, Any]]],
        cleaned_by_row: dict[int, dict[str, Any]],
        status_by_row: dict[int, tuple[str, str | None]],
    ) -> list[LineOutcome]:
        """One verdict per source line, for the preview and the report.

        Identity is read from the cleaned record where the row got that far and
        from the raw canonical record where it did not — a row rejected for a
        bad date still has an invoice number, and naming it is the whole point
        of the report.
        """
        first_reason: dict[int, Rejection] = {}
        for rejection in self.rejections:
            first_reason.setdefault(rejection.row_number, rejection)

        reported = [name for name in LINE_REPORT_FIELDS if name in self.spec.field_map]
        outcomes: list[LineOutcome] = []
        for source_row, record in rows[:LINE_REPORT_LIMIT]:
            number = source_row.row_number
            source = cleaned_by_row.get(number, record)
            status, error_code = status_by_row.get(number, (STAGING_REJECTED, None))
            volume = self._volume_by_row.get(number)
            rejection = first_reason.get(number)
            outcomes.append(LineOutcome(
                row_number=number,
                status=ACCEPTED if status == STAGING_VALID else REJECTED,
                reason=rejection.message if rejection else None,
                fields={name: source.get(name) for name in reported},
                volume=volume,
                error_code=(rejection.error.code if rejection else error_code),
            ))
        return outcomes

    def _reject(self, row: SourceRow, error: ErrorSpec, message: str,
                field_name: str | None = None, field_value: Any = None) -> None:
        """Step 12: record a rejection. Never raises, never discards the row."""
        self.rejections.append(Rejection(
            row_number=row.row_number,
            error=error,
            message=message,
            raw_data={str(k): _as_text(v) for k, v in row.values.items()},
            field_name=field_name,
            field_value=None if field_value is None else str(field_value),
        ))

    def _persist_rejections(self) -> None:
        assert self.batch is not None
        if not self.rejections:
            return
        payload = [
            {
                "batch_id": self.batch.batch_id,
                "data_type": self.spec.data_type,
                "source_file": self.reader.source_name,
                "source_row_number": rejection.row_number,
                "raw_data": rejection.raw_data,
                "error_code": rejection.error.code,
                "error_category": rejection.error.category,
                "error_message": rejection.message,
                "field_name": rejection.field_name,
                "field_value": rejection.field_value,
            }
            for rejection in self.rejections
        ]
        bulk_insert(self.session, EtlRejectedRecord.__table__, payload)

    def _update_staging_status(self, status_by_row: dict[int, tuple[str, str | None]]) -> None:
        """Write each staged row's verdict back so staging is self-describing."""
        assert self.batch is not None
        if not status_by_row:
            return
        table = self.staging_model.__table__
        grouped: dict[tuple[str, str | None], list[int]] = {}
        for row_number, verdict in status_by_row.items():
            grouped.setdefault(verdict, []).append(row_number)
        # Chunked for the same reason ``bulk`` chunks its statements: the row
        # numbers become bind parameters, and one ``IN`` holding every row of a
        # large file blows SQLite's 32,766-parameter ceiling outright. The budget
        # is the dialect's, less a small allowance for the statement's own
        # parameters (batch id, status, error).
        size = max(1, parameter_limit(self.session) - 8)
        for (status, error), row_numbers in grouped.items():
            for chunk in chunked(row_numbers, size):
                self.session.execute(
                    table.update()
                    .where(table.c.import_batch_id == self.batch.batch_id)
                    .where(table.c.source_row_number.in_(chunk))
                    .values(validation_status=status, validation_error=error)
                )

    # -- orchestration ------------------------------------------------------

    def run(self) -> ImportResult:
        batch = self._create_batch()
        result = ImportResult(
            batch_id=batch.batch_id,
            batch_uuid=batch.batch_uuid,
            data_type=self.spec.data_type,
            source_type=self.reader.source_type,
            source_system=self.source_system,
            source_file=self.reader.source_name,
            load_mode=self.load_mode,
        )

        header_map, unmapped, missing = self._detect_structure()
        result.unmapped_columns = unmapped
        result.missing_columns = missing

        if missing:
            batch.status = BATCH_FAILED
            batch.completed_at = datetime.now(timezone.utc)
            batch.error_summary = {
                "error_code": errors.MISSING_SOURCE_COLUMN.code,
                "missing_columns": missing,
                "available_columns": self.reader.headers,
            }
            result.status = BATCH_FAILED
            result.message = (
                "Required column(s) missing from the source: " + ", ".join(missing) +
                ". Nothing was loaded."
            )
            self.progress.finish(message=result.message, failed=True)
            self.session.flush()
            return result

        # Step 3 (continued): read the file. The row count is not known until the
        # last row is in hand, so this phase reports rows read rather than a
        # fraction — there is no honest denominator yet.
        # READING belongs to whoever does the reading. A file reader announces it
        # as it parses; a reader handed to this pipeline ready-made — the
        # in-memory one behind ``/api/import/*``, or any caller that wired no
        # reporter into its own — never does, and that band would then be one
        # nothing ever fills. So it is claimed here only if it is still unspent.
        # Announcing it unconditionally would recompute the bar from the phase's
        # start and walk it back over ground the reader had already covered.
        if not self.progress.entered(Phase.READING):
            self.progress.phase(Phase.READING)
        rows: list[tuple[SourceRow, dict[str, Any]]] = []
        for source_row in self.reader:
            rows.append((source_row, self._to_canonical(source_row, header_map)))
            if len(rows) % 500 == 0:
                self.progress.counts(total_records=len(rows),
                                     processed_records=len(rows))
        result.total_rows = len(rows)
        batch.total_rows = len(rows)
        batch.status = BATCH_VALIDATING
        self.session.flush()

        # Staging is a bulk insert of every source row and is a real part of the
        # wait, so it drives a phase of its own to the end of its share. It used
        # to re-enter READING for this, which worked only while nothing else
        # reported that phase; the reader does now, so the two would have been
        # competing for one band and the insert would have had none of it left.
        self.progress.phase(Phase.STAGING, total=len(rows))
        self._load_staging(rows)

        # Steps 5, 8, 9: required fields, dates and numerics, row by row.
        #
        # Cleaning and master mapping are two passes rather than one loop so that
        # each can be reported as the distinct thing it is — a file can be
        # perfectly well-formed and still fail entirely on master codes, and the
        # operator watching the bar needs to see which of the two is happening.
        # It costs nothing: ``cleaned_by_row`` already retained every cleaned row
        # for the line report, so the second pass reads what the first produced.
        self.progress.phase(Phase.VALIDATING, total=len(rows))
        cleaned_by_row: dict[int, dict[str, Any]] = {}
        cleaned_pairs: list[tuple[SourceRow, dict[str, Any]]] = []
        for position, (source_row, record) in enumerate(rows, start=1):
            cleaned = self._clean_record(record, source_row)
            if cleaned is not None:
                cleaned_by_row[source_row.row_number] = cleaned
                cleaned_pairs.append((source_row, cleaned))
            if self.progress.every(position, len(rows)):
                self.progress.rows(
                    position, valid=len(cleaned_pairs),
                    invalid=len({r.row_number for r in self.rejections}),
                )

        # Steps 6 and 7: master codes and hierarchy. The index is built once for
        # the whole file, so it is loaded here rather than per row.
        self.progress.phase(Phase.MAPPING, total=len(cleaned_pairs))
        index = self._master_index or MasterDataIndex(self.session)

        cleaned_rows: list[tuple[SourceRow, dict[str, Any], dict[str, Any]]] = []
        for position, (source_row, cleaned) in enumerate(cleaned_pairs, start=1):
            dimensions = self._map_master(cleaned, source_row, index)
            if dimensions is not None:
                cleaned_rows.append((source_row, cleaned, dimensions))
            if self.progress.every(position, len(cleaned_pairs)):
                self.progress.rows(
                    position, valid=len(cleaned_rows),
                    invalid=len({r.row_number for r in self.rejections}),
                )

        # Step 10: duplicates, first inside the file then against the warehouse.
        self.progress.phase(Phase.IMPORTING, total=len(cleaned_rows))
        status_by_row: dict[int, tuple[str, str | None]] = {}
        seen: dict[str, int] = {}
        repeats: dict[str, int] = {}
        reject_repeats = get_settings().etl_reject_duplicate_in_file
        candidates: list[tuple[SourceRow, dict[str, Any], dict[str, Any], str]] = []
        for source_row, cleaned, dimensions in cleaned_rows:
            key = self.spec.build_business_key(cleaned, self.source_system)
            if key in seen:
                if reject_repeats:
                    self._reject(
                        source_row, errors.DUPLICATE_IN_FILE,
                        "Duplicate line found within uploaded file: "
                        f"{self._describe_line(cleaned)} already appears at row "
                        f"{seen[key]}.",
                        field_value=key,
                    )
                    status_by_row[source_row.row_number] = (
                        STAGING_DUPLICATE, errors.DUPLICATE_IN_FILE.code
                    )
                    continue
                # The check is off, so the repeat loads. It cannot load under
                # the key its twin already holds: `business_key` is UNIQUE, and
                # the upsert applies a repeated key as two parameter sets that
                # resolve to whichever was written last — one line's figures
                # would silently replace the other's. Numbering the repeat keeps
                # both lines, and keeps a re-upload idempotent, because the same
                # file yields the same keys in the same order.
                repeats[key] = repeats.get(key, 1) + 1
                key = f"{key}#{repeats[key]}"
            seen[key] = source_row.row_number
            candidates.append((source_row, cleaned, dimensions, key))

        fact_table = self.fact_model.__table__
        already_loaded = existing_keys(
            self.session, fact_table, "business_key", [c[3] for c in candidates]
        )

        fact_rows: list[dict[str, Any]] = []
        needed_dates: set = set()
        for position, (source_row, cleaned, dimensions, key) in enumerate(
            candidates, start=1
        ):
            if key in already_loaded:
                result.duplicate_rows += 1
                if self.load_mode == LOAD_MODE_INITIAL:
                    self._reject(
                        source_row, errors.DUPLICATE_IN_WAREHOUSE,
                        "Duplicate transaction line: "
                        f"{self._describe_line(cleaned)} is already in the "
                        "warehouse and the load mode is INITIAL, so it is not "
                        "re-applied.",
                        field_value=key,
                    )
                    status_by_row[source_row.row_number] = (
                        STAGING_DUPLICATE, errors.DUPLICATE_IN_WAREHOUSE.code
                    )
                    continue
            # Resolved here rather than inside the fact builder because a
            # negative volume rejects the line, and by the time the builder has
            # run the row is already on its way into the fact table.
            volume: VolumeResult | None = None
            if "volume" in fact_table.columns.keys():
                volume = self._volume_for(cleaned, source_row)
                if volume is None:
                    status_by_row[source_row.row_number] = (
                        STAGING_REJECTED, errors.VOLUME_NEGATIVE.code
                    )
                    continue

            if self.spec.date_field is not None:
                needed_dates.add(cleaned[self.spec.date_field])
            # Every other date the row states needs its ``dim_date`` entry too,
            # or the foreign key fails at the write — after the whole file has
            # been read and validated, which is the most expensive moment to
            # discover it.
            for field_name, _column in self.spec.date_columns:
                value = cleaned.get(field_name)
                if value is not None:
                    needed_dates.add(value)
            fact_rows.append(
                self._build_fact_row(cleaned, dimensions, source_row, key, volume)
            )
            status_by_row[source_row.row_number] = (STAGING_VALID, None)
            if self.progress.every(position, len(candidates)):
                self.progress.rows(
                    position, valid=len(fact_rows),
                    invalid=len({r.row_number for r in self.rejections}),
                )

        ensure_dates_exist(self.session, needed_dates)

        # Whatever the dataset wants said about the load as a whole. Run over the
        # rows that survived validation, because a check against rows the loader
        # refused would be comparing the source's figure with a total this load
        # never claimed.
        note_builder = LOAD_NOTES.get(self.spec.data_type)
        if note_builder is not None:
            result.notes.extend(note_builder([c[1] for c in candidates]))

        # The scope as it stands *before* this load touches it. See
        # ``_scope_snapshot``: read after the write, a newly inserted row and a
        # restated one are the same thing.
        scope_before = self._scope_snapshot() if self.restatement_scope else {}

        # Step 11: the write. Reported chunk by chunk because on a large file
        # this is where most of the wait actually is.
        self.progress.phase(Phase.WRITING, total=len(fact_rows))
        inserted, updated = bulk_upsert(
            self.session, fact_table, fact_rows, conflict_column="business_key",
            on_chunk=self.progress.rows,
        )
        self.progress.counts(imported_records=inserted + updated)

        # Step 12: the restatement. After the write, because what is voided is
        # defined as "in scope and not written by this load", so the write has to
        # have happened for the question to have an answer. A dry run reaches
        # here too and rolls the whole thing back, which is what lets the preview
        # state the consequence before anybody commits to it.
        if self.restatement_scope:
            result.restatement = self._restate(
                scope_before,
                {self._identity(c[1]) for c in candidates},
                {c[3] for c in candidates})

        for rejection in self.rejections:
            status_by_row.setdefault(
                rejection.row_number, (STAGING_REJECTED, rejection.error.code)
            )
        self._update_staging_status(status_by_row)
        self._persist_rejections()
        result.lines = self._line_outcomes(rows, cleaned_by_row, status_by_row)

        result.valid_rows = len(fact_rows)
        # Rows, not rejection records: a single row can legitimately carry more
        # than one reason (two invalid codes, say), and the error breakdown
        # counts reasons while this counts rows that failed.
        result.rejected_rows = len({r.row_number for r in self.rejections})
        result.inserted_rows = inserted
        result.updated_rows = updated
        result.error_counts = _count(r.error.code for r in self.rejections)
        result.category_counts = _count(r.error.category for r in self.rejections)
        result.rejections = list(self.rejections)
        result.volume_gaps = dict(self.volume_gaps)
        result.hierarchy_from_customer = self.hierarchy_from_customer
        result.deduction_convention = self.deduction_convention

        batch.successful_rows = result.valid_rows
        batch.failed_rows = result.rejected_rows
        batch.duplicate_rows = result.duplicate_rows
        batch.inserted_rows = inserted
        batch.updated_rows = updated
        batch.completed_at = datetime.now(timezone.utc)
        batch.status = (
            BATCH_COMPLETED_WITH_ERRORS if self.rejections else BATCH_COMPLETED
        )
        # ``volume_gaps`` earns its place here even though it rejects nothing:
        # it is the only record of *why* a loaded row has no volume, and the
        # data-quality report reads it back long after the run.
        batch.error_summary = {
            "by_error_code": result.error_counts,
            "by_category": result.category_counts,
            "unmapped_columns": unmapped,
            "volume_gaps": result.volume_gaps,
        } if (self.rejections or unmapped or self.volume_gaps) else None
        result.status = batch.status
        self.progress.counts(
            total_records=result.total_rows,
            processed_records=result.total_rows,
            valid_records=result.valid_rows,
            invalid_records=result.rejected_rows,
            imported_records=inserted + updated,
            failed_records=result.rejected_rows,
        )
        self.session.flush()
        return result


def _as_text(value: Any) -> str | None:
    """Staging keeps everything as text so a bad value stays visible."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _count(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def run_import(
    engine: Engine,
    data_type: str,
    source: str | Path | SourceReader,
    *,
    source_system: str = "MANUAL",
    load_mode: str = LOAD_MODE_INCREMENTAL,
    date_format: str | None = None,
    sheet_name: str | None = None,
    dry_run: bool = False,
    progress: ProgressReporter | None = None,
    deduction_convention: str | None = None,
    restatement_scope: Sequence[str] | None = None,
    restatement_hold: tuple[str, Sequence[str]] | None = None,
) -> ImportResult:
    """Import one transaction file (or reader) end to end.

    The whole run is one transaction: either the batch, staging rows, facts and
    rejections are all committed, or nothing is. ``dry_run`` performs every step
    and rolls back, which is the safe way to inspect a new file's data quality —
    and, for a restating load, the only way to see what it is about to stand down
    before it stands it down.

    ``deduction_convention`` says how this file signs a payment, a discount and
    an adjustment; a dataset that needs one and is not given one refuses to load.
    ``restatement_scope`` declares what this file states *in full*: rows inside
    it that the file does not name are voided. Both are declarations about the
    upload rather than properties of the dataset, and neither is ever inferred
    from the file's own contents — see ``DatasetSpec`` for why the second of
    those is the more dangerous of the two to get wrong.
    """
    # A reader built here is handed the reporter as well, so a run given a path
    # reports its read exactly as one given a ready-made reader does. A caller
    # that supplied its own reader has already wired it up, or deliberately has
    # not.
    reader = source if isinstance(source, SourceReader) else reader_for_file(
        source, sheet_name=sheet_name, progress=progress
    )

    session = Session(bind=engine, expire_on_commit=False, future=True)
    try:
        ensure_master_source_status(session)
        pipeline = EtlPipeline(
            session, data_type, reader,
            source_system=source_system, load_mode=load_mode, date_format=date_format,
            progress=progress, deduction_convention=deduction_convention,
            restatement_scope=restatement_scope,
            restatement_hold=restatement_hold,
        )
        result = pipeline.run()
        if dry_run:
            session.rollback()
            result.message = ((result.message or "") +
                              " Dry run: every change was rolled back.").strip()
            result.batch_id = None
        else:
            session.commit()
        return result
    except Exception:
        # Covers ``ImportCancelled`` as much as any failure, and deliberately so:
        # the run is one transaction, and rolling it back is what guarantees a
        # cancelled import leaves no half-loaded rows behind. The exception is
        # re-raised rather than turned into a result, because a caller that
        # received a result would have no way to tell a stopped run from a
        # finished one.
        session.rollback()
        raise
    finally:
        session.close()


__all__ = [
    "EtlPipeline",
    "ImportResult",
    "Restatement",
    "LineOutcome",
    "Rejection",
    "run_import",
    "ACCEPTED",
    "REJECTED",
    "LINE_REPORT_FIELDS",
    "LINE_REPORT_LIMIT",
    "LOAD_MODE_INITIAL",
    "LOAD_MODE_INCREMENTAL",
    "LOAD_MODE_REPROCESS",
]
