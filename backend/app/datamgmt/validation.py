"""Validating one edited record.

A record typed into a form has to satisfy exactly what a record loaded from a
file satisfies, so this reuses the upload centre's rules rather than restating
them: the same cleaner coerces the value, the same required check applies, the
same parent-existence check runs. Three things are added, because they only
arise when editing an existing record rather than loading a new one:

**Duplicate code.** Creating a record whose code is already taken is a conflict,
not an update — the management form is not an upsert.

**Hierarchy consistency.** A file's rows name one parent each, and the ETL
derives the rest of the chain from it. A form can offer the whole chain at once,
which makes it possible to say "territory T001, in region Khulna" when T001
lives under Dhaka. That contradiction is caught here, against the real master
hierarchy, and reported against the field that is wrong.

**Coordinates.** Latitude and longitude are bounded, and the bounds are the
map module's, not a second copy of them.

Errors are :class:`UploadIssue` values — the same shape the upload preview
renders — so the frontend has one way to display a validation problem.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..etl.mapping import BINDING_BY_LEVEL, MasterDataIndex
from ..upload.errors import UploadIssue, code
from ..upload.master_loader import clean_value
from ..upload.registry import MASTER_MODEL_BY_TABLE, UploadColumn
from ..utils.text import is_blank
from .catalogue import ManagedEntity, ManagedField

#: The organisational level each master table represents, for the hierarchy
#: check. Mirrors ``etl.mapping``'s bindings rather than inventing an order.
_LEVEL_BY_TABLE: dict[str, str] = {
    "dim_company": "company_code",
    "dim_business_unit": "bu_code",
    "dim_sales_line": "sales_line_code",
    "dim_zone": "zone_code",
    "dim_region": "region_code",
    "dim_area": "area_code",
    "dim_unit": "unit_code",
    "dim_territory": "territory_code",
    "dim_sub_territory": "sub_territory_code",
}

LATITUDE_RANGE = (-90.0, 90.0)
LONGITUDE_RANGE = (-180.0, 180.0)


class ValidationFailed(Exception):
    """One or more fields are wrong. Carries every problem, not just the first.

    Reporting all of them matters for a form: fixing one field, resubmitting,
    and being told about the next is a poor way to learn that three were wrong.
    """

    def __init__(self, issues: list[UploadIssue]) -> None:
        super().__init__(f"{len(issues)} validation problem(s)")
        self.issues = issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "detail": "The record could not be saved because some fields are invalid.",
            "errors": [issue.to_dict() for issue in self.issues],
        }


def _as_upload_column(field: ManagedField) -> UploadColumn:
    """Adapt a managed field to what the shared cleaner expects."""
    return UploadColumn(
        name=field.label, target=field.name, kind=field.kind,
        required=field.required, description=field.description,
    )


def clean_and_validate(session: Session, entity: ManagedEntity,
                       values: dict[str, Any], *, creating: bool,
                       existing: Any = None) -> dict[str, Any]:
    """Coerce and check a submitted record; return the values to store.

    Only fields present in ``values`` are touched, so a partial edit updates
    what it names and leaves the rest of the record alone.
    """
    issues: list[UploadIssue] = []
    cleaned: dict[str, Any] = {}
    by_name = entity.field_by_name

    for name, raw in values.items():
        field = by_name.get(name)
        if field is None:
            issues.append(UploadIssue(
                row_number=None, column=name, value=_text(raw),
                error_code=code.INVALID_TYPE,
                message=f"'{entity.label}' has no field '{name}'.",
                suggested_fix="Remove the field, or check the spelling against "
                              "the record's fields.",
            ))
            continue
        if not field.editable and not (creating and field.is_key):
            issues.append(UploadIssue(
                row_number=None, column=field.label, value=_text(raw),
                error_code=code.INVALID_TYPE,
                message=(
                    f"{field.label} cannot be changed. The business code "
                    "identifies this record on every transaction that "
                    "references it."
                    if field.is_key else
                    f"{field.label} is set by the import pipeline and cannot be "
                    "edited here."
                ),
                suggested_fix="Leave this field as it is.",
            ))
            continue

        value, error = clean_value(_as_upload_column(field), raw)
        if error:
            issues.append(UploadIssue(
                row_number=None, column=field.label, value=_text(raw),
                error_code=code.INVALID_TYPE,
                message=f"{field.label}: {error}.",
                suggested_fix=_as_upload_column(field).guidance,
            ))
            continue
        cleaned[name] = value

    issues += _check_required(entity, cleaned, creating=creating,
                              existing=existing)
    issues += _check_choices(entity, cleaned)
    issues += _check_coordinates(cleaned)

    if creating:
        issues += _check_duplicate(session, entity, cleaned)
    issues += _check_parent(session, entity, cleaned)
    issues += _check_lookups(session, entity, cleaned)
    issues += _check_level_references(session, entity, cleaned)
    issues += _check_hierarchy(session, entity, cleaned, existing)

    if issues:
        raise ValidationFailed(issues)
    return cleaned


def _check_required(entity: ManagedEntity, cleaned: dict[str, Any], *,
                    creating: bool, existing: Any) -> list[UploadIssue]:
    """A required field may be absent from an edit, but never blanked by one."""
    issues = []
    for field in entity.fields:
        if not field.required:
            continue
        if field.name not in cleaned:
            if creating:
                issues.append(UploadIssue(
                    row_number=None, column=field.label, value=None,
                    error_code=code.MISSING_REQUIRED,
                    message=f"{field.label} is required.",
                    suggested_fix=f"Enter a value. {field.description}".strip(),
                ))
            continue
        if is_blank(cleaned[field.name]):
            issues.append(UploadIssue(
                row_number=None, column=field.label, value=None,
                error_code=code.MISSING_REQUIRED,
                message=f"{field.label} is required and cannot be cleared.",
                suggested_fix=f"Enter a value. {field.description}".strip(),
            ))
    return issues


def _check_choices(entity: ManagedEntity, cleaned: dict[str, Any],
                   ) -> list[UploadIssue]:
    """Constrained fields accept their own values, case-insensitively.

    ``status`` is free text in the source data, so an existing "ACTIVE" must
    keep working; what is refused is a value that is neither.
    """
    issues = []
    for field in entity.fields:
        value = cleaned.get(field.name)
        if not field.choices or is_blank(value):
            continue
        if str(value).strip().lower() not in {c.lower() for c in field.choices}:
            issues.append(UploadIssue(
                row_number=None, column=field.label, value=_text(value),
                error_code=code.INVALID_TYPE,
                message=f"{field.label} must be one of "
                        f"{', '.join(field.choices)}.",
                suggested_fix=f"Use {' or '.join(field.choices)}.",
            ))
    return issues


def _check_coordinates(cleaned: dict[str, Any]) -> list[UploadIssue]:
    issues = []
    for name, (low, high) in (("latitude", LATITUDE_RANGE),
                              ("longitude", LONGITUDE_RANGE)):
        value = cleaned.get(name)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue        # already reported by the type check
        if not low <= number <= high:
            issues.append(UploadIssue(
                row_number=None, column=name.title(), value=_text(value),
                error_code=code.INVALID_TYPE,
                message=f"{name.title()} must be between {low} and {high}.",
                suggested_fix=f"Enter decimal degrees between {low} and {high}.",
            ))
    return issues


def _check_duplicate(session: Session, entity: ManagedEntity,
                     cleaned: dict[str, Any]) -> list[UploadIssue]:
    key_field = entity.key_fields[0]
    value = cleaned.get(key_field)
    if is_blank(value):
        return []
    exists = session.execute(
        select(func.count()).select_from(entity.model.__table__)
        .where(getattr(entity.model, key_field) == value)
    ).scalar_one()
    if not exists:
        return []
    field = entity.field_by_name[key_field]
    return [UploadIssue(
        row_number=None, column=field.label, value=_text(value),
        error_code=code.ALREADY_EXISTS,
        message=f"{field.label} '{value}' already exists.",
        # Including the retired case, because a code taken by a retired record
        # is still taken, and "restore it" is the right advice.
        suggested_fix="Use a different code, or open the existing record — it "
                      "may be retired and need restoring rather than recreating.",
    )]


def _check_parent(session: Session, entity: ManagedEntity,
                  cleaned: dict[str, Any]) -> list[UploadIssue]:
    """The immediate parent must exist. Same rule as the upload loader."""
    column = entity.parent_column
    if not column or column not in cleaned:
        return []
    value = cleaned[column]
    if is_blank(value):
        return []
    parent_model = MASTER_MODEL_BY_TABLE.get(entity.parent_table or "")
    if parent_model is None:
        return []
    exists = session.execute(
        select(func.count()).select_from(parent_model.__table__)
        .where(getattr(parent_model, column) == value)
    ).scalar_one()
    if exists:
        return []
    field = entity.field_by_name.get(column)
    return [UploadIssue(
        row_number=None, column=field.label if field else column,
        value=_text(value), error_code=code.INVALID_PARENT,
        message=f"{column.replace('_', ' ')} '{value}' does not exist in "
                f"{entity.parent_table}.",
        suggested_fix=f"Choose a {column.replace('_code', '').replace('_', ' ')} "
                      "that already exists.",
    )]


def _check_lookups(session: Session, entity: ManagedEntity,
                   cleaned: dict[str, Any]) -> list[UploadIssue]:
    """Declared references must name an existing record.

    Reads the same :data:`app.upload.registry.LOOKUPS_BY_TABLE` the upload path
    reads, so typing a bad code into the form and putting the same bad code in
    a spreadsheet produce the same refusal with the same wording.
    """
    from ..upload.registry import LOOKUPS_BY_TABLE, MASTER_MODEL_BY_TABLE

    issues = []
    for lookup in LOOKUPS_BY_TABLE.get(entity.table or "", ()):
        if lookup.column not in cleaned:
            continue
        value = cleaned[lookup.column]
        if is_blank(value):
            continue        # optional: an unmapped record is a legitimate one
        model = MASTER_MODEL_BY_TABLE.get(lookup.table)
        if model is None:
            continue
        exists = session.execute(
            select(func.count()).select_from(model.__table__)
            .where(getattr(model, lookup.target_column) == value)
        ).scalar_one()
        if exists:
            continue
        field = entity.field_by_name.get(lookup.column)
        issues.append(UploadIssue(
            row_number=None, column=field.label if field else lookup.column,
            value=_text(value), error_code=code.INVALID_PARENT,
            message=f"Invalid {lookup.label}: {value}",
            suggested_fix=(
                f"Choose a {lookup.label.lower()} that already exists, or clear "
                "the field to leave the record unmapped."
            ),
        ))
    return issues


def _check_level_references(session: Session, entity: ManagedEntity,
                            cleaned: dict[str, Any]) -> list[UploadIssue]:
    """Any organisational code the record names must exist.

    The declared parent is checked above; this catches the *other* kind of
    hierarchy reference — a column pointing at a level that is not this
    dimension's parent. ``dim_sales_force.territory_code`` is the live example:
    the registry documents it as "must exist in dim_territory when supplied",
    but it is deliberately not a foreign key, because until the real sales-force
    master arrives nothing guarantees its codes match the official hierarchy.
    Not being a constraint is exactly why it needs a check.
    """
    from ..upload.registry import LOOKUPS_BY_TABLE

    # A column already checked as a declared lookup is not checked twice; one
    # wrong code should produce one message.
    already = {l.column for l in LOOKUPS_BY_TABLE.get(entity.table or "", ())}

    issues = []
    for name, value in cleaned.items():
        if name == entity.parent_column or name in entity.key_fields:
            continue
        if name in already:
            continue
        if name not in BINDING_BY_LEVEL or is_blank(value):
            continue
        binding = BINDING_BY_LEVEL[name]
        exists = session.execute(
            select(func.count()).select_from(binding.model.__table__)
            .where(getattr(binding.model, name) == value)
        ).scalar_one()
        if exists:
            continue
        field = entity.field_by_name.get(name)
        issues.append(UploadIssue(
            row_number=None, column=field.label if field else name,
            value=_text(value), error_code=code.INVALID_PARENT,
            message=(
                f"{name.replace('_code', '').replace('_', ' ')} '{value}' does "
                f"not exist in {binding.model.__tablename__}."
            ),
            suggested_fix=f"Choose a "
                          f"{name.replace('_code', '').replace('_', ' ')} that "
                          "already exists in the master data.",
        ))
    return issues


def _check_hierarchy(session: Session, entity: ManagedEntity,
                     cleaned: dict[str, Any], existing: Any,
                     ) -> list[UploadIssue]:
    """Every organisational code on the record must be one chain, not several.

    A dimension row normally carries only its immediate parent, so there is
    nothing to contradict — the check is silent for those. It matters for a
    record that names two levels at once, where the pair can disagree: the
    parent is walked up through ``MasterDataIndex`` — the same index the ETL and
    the permission filter use — and any other level the record names that lands
    somewhere else is reported against the field that is wrong.
    """
    level = _LEVEL_BY_TABLE.get(entity.table or "")
    if level is None or not entity.parent_column:
        return []

    parent_code = cleaned.get(entity.parent_column)
    if is_blank(parent_code) and existing is not None:
        parent_code = getattr(existing, entity.parent_column, None)
    if is_blank(parent_code):
        return []

    parent_level = entity.parent_column
    if parent_level not in BINDING_BY_LEVEL:
        return []

    index = MasterDataIndex(session)
    ancestors = dict(index.ancestors_of(parent_level, str(parent_code)))
    if not ancestors:
        return []           # the parent itself is missing; already reported

    issues = []
    for name, value in cleaned.items():
        if name in (entity.parent_column, *entity.key_fields):
            continue
        if name not in BINDING_BY_LEVEL or is_blank(value):
            continue
        expected = ancestors.get(name)
        if expected is not None and str(value) != str(expected):
            field = entity.field_by_name.get(name)
            issues.append(UploadIssue(
                row_number=None, column=field.label if field else name,
                value=_text(value), error_code="HIERARCHY_MISMATCH",
                message=(
                    f"{name.replace('_code', '').replace('_', ' ')} '{value}' "
                    f"does not match the hierarchy: {parent_code} sits under "
                    f"'{expected}'."
                ),
                suggested_fix=f"Use '{expected}', or change the "
                              f"{entity.parent_column.replace('_', ' ')} instead.",
            ))
    return issues


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)[:200]


__all__ = ["ValidationFailed", "clean_and_validate", "LATITUDE_RANGE",
           "LONGITUDE_RANGE"]
