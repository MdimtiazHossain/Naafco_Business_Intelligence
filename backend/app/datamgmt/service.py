"""The write operations: create, update, retire, restore, void, bulk.

Every one of them follows the same five steps, in this order:

1. **Find** the record, and refuse a missing one with a 404 rather than a
   silently empty update.
2. **Scope-check** it. A record outside the caller's data scope is not theirs to
   change, and addressing it directly must not be a way round the table filter.
3. **Validate** the submitted values against the same rules an uploaded file
   meets.
4. **Apply** the change — and for anything destructive, apply it as a flag, not
   a ``DELETE``.
5. **Record** it: the field-level diff to the change log, the event to the audit
   trail.

The caller owns the transaction. Nothing here commits, so a route that fails
after calling one of these leaves no half-written change behind.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.permission_filter import UserContext
from ..database.models_admin import ChangeAction
from . import dependencies, history, scope
from .catalogue import STATUS_VALUES, ManagedEntity
from .query import (
    KEY_SEPARATOR,
    get_master_record,
    get_transaction_record,
    removal_refusal,
)
from .validation import ValidationFailed, clean_and_validate


class RecordNotFound(LookupError):
    """No such record — or none this caller may see."""


class ScopeDenied(PermissionError):
    """The record exists but lies outside the caller's data scope."""


class OperationRefused(Exception):
    """The operation is legitimate in general but not for this record.

    Carries the dependants that caused the refusal so the UI can list them
    rather than repeating a bare message.
    """

    def __init__(self, message: str,
                 dependants: dependencies.Dependants | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.dependants = dependants


@dataclass
class WriteResult:
    """What a write did, as the API reports it back."""

    record: dict[str, Any]
    action: str
    changed_fields: list[str]
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "record": self.record,
            "action": self.action,
            "changed_fields": self.changed_fields,
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------


def _label_of(entity: ManagedEntity, record: Any) -> str | None:
    if entity.label_field:
        return getattr(record, entity.label_field, None)
    return None


def _code_of(entity: ManagedEntity, record: Any) -> str:
    """The record's identity as one string — every key column, not just the first.

    This is what addresses the record in a URL, in the change log and in the
    audit trail, so for a composite-key entity it has to name the whole key or
    two different records would share one history.
    """
    return KEY_SEPARATOR.join(
        str(getattr(record, name)) for name in entity.key_fields
    )


def _code_from_values(entity: ManagedEntity, cleaned: dict[str, Any]) -> str:
    return KEY_SEPARATOR.join(
        str(cleaned.get(name, "")) for name in entity.key_fields
    )


def _snapshot(entity: ManagedEntity, record: Any) -> dict[str, Any]:
    return {f.name: getattr(record, f.name, None) for f in entity.fields}


def _as_row(entity: ManagedEntity, record: Any) -> dict[str, Any]:
    from .query import master_row

    return master_row(entity, record)


# ---------------------------------------------------------------------------
# Per-table work that another module owns
# ---------------------------------------------------------------------------


def _after_location_write(session: Session, record: Any, user: UserContext,
                          action: str, changed: list[str]) -> str | None:
    """Keep a hand-edited coordinate hand-edited, then re-derive the levels above.

    Two things the generic write cannot know, both owned by :mod:`app.map.geo`.

    **Provenance.** ``derive_parents`` recomputes every ``DERIVED`` row and
    leaves the rest alone, which is what lets somebody place a region's real
    head office and keep it. A coordinate corrected on this screen was placed by
    a person, so it is marked ``MANUAL`` and its ``derived_from`` count cleared —
    without that, a row that arrived as a centroid would be edited here and
    silently recomputed back on the next upload, and the reader would have no
    way to tell why their correction kept disappearing.

    **Derivation.** Everything above the placed levels is a centroid of what
    sits below, so moving, adding or removing one coordinate changes the ones
    above it. This is the same call ``upload.master_loader.POST_LOAD`` makes
    after a coordinate file lands, for the same reason.

    Removing a placed coordinate from a level that has coordinates *below* it
    does not leave the entity unplaced — the derivation immediately gives it
    the centroid of its children, which is the right answer and looks exactly
    like the delete having failed. So the note this returns says so, and the
    caller puts it in the message: the alternative is a reader watching a row
    they just removed still sitting in the table with no explanation.
    """
    from ..map.geo import derive_parents
    from ..database.models_map import GeoSource, MapEntityLocation

    if action != ChangeAction.DELETED:
        moved = {"latitude", "longitude"} & set(changed)
        if moved or action == ChangeAction.CREATED:
            record.source = GeoSource.MANUAL
            record.derived_from = None
        record.updated_by = user.username
        session.flush()
        derive_parents(session, actor=user.username)
        return None

    entity_type, entity_code = record.entity_type, record.entity_code
    derive_parents(session, actor=user.username)

    replacement = session.execute(
        select(MapEntityLocation).where(
            MapEntityLocation.entity_type == entity_type,
            MapEntityLocation.entity_code == entity_code,
        )
    ).scalars().first()
    if replacement is None:
        return None
    return (
        f" {entity_type.replace('_', ' ')} '{entity_code}' is still on the map: "
        f"with the placed coordinate gone it falls back to the centroid of the "
        f"{replacement.derived_from or 0} coordinate(s) below it. Remove those "
        "to take it off entirely."
    )


#: Work to run after a master write, by table.
#:
#: Hand-written and deliberately small: it exists for the case where another
#: module owns a rule about the row that the generic write path cannot infer
#: from the column list. Mirrors ``upload.master_loader.POST_LOAD``, which does
#: the same job on the same table for the upload path — the two entry points
#: into a coordinate therefore leave the warehouse in the same state.
_AFTER_MASTER_WRITE: dict[str, Any] = {
    "map_entity_locations": _after_location_write,
}


def _after_write(session: Session, entity: ManagedEntity, record: Any,
                 user: UserContext, action: str,
                 changed: list[str]) -> str | None:
    """Run the table's hook, and hand back anything it needs the caller to say."""
    hook = _AFTER_MASTER_WRITE.get(entity.table or "")
    if hook is None:
        return None
    return hook(session, record, user, action, changed)


def _check_parent_scope(session: Session, user: UserContext,
                        entity: ManagedEntity, cleaned: dict[str, Any]) -> None:
    """A record may not be moved to a parent outside the caller's scope.

    Without this, reparenting would be a hole straight through the data scope: a
    Dhaka manager could move their own territory under a Khulna unit, and the
    territory — still theirs to edit a moment ago — would carry Dhaka's history
    into a region they were never allowed to touch.
    """
    from ..ai.permission_filter import PermissionFilter
    from ..etl.mapping import BINDING_BY_LEVEL

    if user.is_unrestricted:
        return
    permissions = PermissionFilter(session, user)
    for name, value in cleaned.items():
        if name not in BINDING_BY_LEVEL or value in (None, ""):
            continue
        if not permissions.is_within_scope(name, str(value)):
            raise ScopeDenied(
                f"You don't have permission to use {name.replace('_', ' ')} "
                f"'{value}'. Your access covers {user.describe_scope()}."
            )


def load_master(session: Session, user: UserContext, entity: ManagedEntity,
                code: str) -> Any:
    """Fetch one master record, enforcing the data scope."""
    record = get_master_record(session, entity, code)
    if record is None:
        raise RecordNotFound(f"No {entity.label} with code '{code}'.")
    if not scope.check_record(session, user, entity, code):
        # Deliberately distinct from "not found": the caller is authenticated
        # and the honest answer is that this is not theirs, which is also what
        # every other endpoint in the platform says.
        raise ScopeDenied(
            f"You don't have permission to access {code}. Your access covers "
            f"{user.describe_scope()}."
        )
    return record


def create_master(session: Session, user: UserContext, entity: ManagedEntity,
                  values: dict[str, Any], *, ip_address: str | None = None,
                  ) -> WriteResult:
    """Create one master record."""
    cleaned = clean_and_validate(session, entity, values, creating=True)
    code = _code_from_values(entity, cleaned)

    if not scope.check_record(session, user, entity, code):
        raise ScopeDenied(
            f"You cannot create {entity.label} '{code}': it lies outside "
            f"{user.describe_scope()}."
        )

    _check_parent_scope(session, user, entity, cleaned)

    record = entity.model(**cleaned)
    session.add(record)
    session.flush()
    _after_write(session, entity, record, user, ChangeAction.CREATED,
                 sorted(cleaned))

    history.record(
        session, entity=entity, record_key=code, label=_label_of(entity, record),
        action=ChangeAction.CREATED, user=user,
        change=history.Change(ChangeAction.CREATED, {},
                              {k: v for k, v in cleaned.items()}),
        ip_address=ip_address,
    )
    return WriteResult(
        record=_as_row(entity, record), action=ChangeAction.CREATED,
        changed_fields=sorted(cleaned),
        message=f"{entity.label} '{code}' created.",
    )


def update_master(session: Session, user: UserContext, entity: ManagedEntity,
                  code: str, values: dict[str, Any], *,
                  reason: str | None = None, ip_address: str | None = None,
                  ) -> WriteResult:
    """Apply an edit to one master record."""
    record = load_master(session, user, entity, code)
    before = _snapshot(entity, record)
    cleaned = clean_and_validate(session, entity, values, creating=False,
                                 existing=record)
    _check_parent_scope(session, user, entity, cleaned)

    change = history.diff(before, cleaned)
    if change.is_empty:
        # Nothing moved. Writing a history line saying so would be noise, and
        # reporting success without one would be a lie about what happened.
        return WriteResult(
            record=_as_row(entity, record), action=ChangeAction.UPDATED,
            changed_fields=[], message="No changes to save.",
        )

    for name in change.fields:
        setattr(record, name, cleaned[name])
    session.flush()
    _after_write(session, entity, record, user, ChangeAction.UPDATED,
                 change.fields)

    history.record(
        session, entity=entity, record_key=code, label=_label_of(entity, record),
        action=ChangeAction.UPDATED, user=user, change=change, reason=reason,
        ip_address=ip_address,
    )
    return WriteResult(
        record=_as_row(entity, record), action=ChangeAction.UPDATED,
        changed_fields=change.fields,
        message=f"{entity.label} '{code}' updated.",
    )


def delete_master(session: Session, user: UserContext, entity: ManagedEntity,
                  code: str, *, reason: str | None = None,
                  ip_address: str | None = None) -> WriteResult:
    """Retire one master record — or remove it, for the few that model no retirement.

    Retiring is the rule and the flag is what keeps every fact that references
    the record resolvable. ``map_entity_locations`` is the exception, and it is
    a coherent one rather than an oversight: the row is a coordinate *about* an
    entity, nothing references it, and removing one leaves the customer or
    territory it pointed at exactly as it was. It has no ``is_deleted`` column
    to set — revision 0034 recreated 0008's table column for column — so the row
    goes, and the change log keeps the whole of it so what was removed is still
    readable afterwards.
    """
    record = load_master(session, user, entity, code)
    if entity.soft_delete and getattr(record, "is_deleted", False):
        raise OperationRefused(f"{entity.label} '{code}' is already retired.")

    # Before anything is written. A record another module recomputes is not
    # this screen's to remove: deleting it would succeed, the after-write hook
    # would derive it again, and the caller would be told a row was gone while
    # looking at it.
    refusal = removal_refusal(entity, record)
    if refusal is not None:
        raise OperationRefused(f"'{code}' cannot be removed. {refusal}")

    # Read before the row goes: a snapshot of a deleted instance is empty.
    snapshot = _snapshot(entity, record)
    label = _label_of(entity, record)
    row = _as_row(entity, record)
    dependants = dependencies.for_master(session, entity, code)

    if entity.soft_delete:
        record.is_deleted = True
        record.deleted_at = history.now()
        record.deleted_by = user.username
        session.flush()
        verb, changed = "retired", ["is_deleted"]
    else:
        session.delete(record)
        session.flush()
        verb, changed = "removed", sorted(snapshot)
    followed = _after_write(session, entity, record, user,
                            ChangeAction.DELETED, changed)

    history.record(
        session, entity=entity, record_key=code, label=label,
        action=ChangeAction.DELETED, user=user,
        # The whole record is kept on a delete, not just a flag: this is the
        # copy someone reads when they need to know what was retired.
        change=history.Change(ChangeAction.DELETED, snapshot, {}),
        reason=reason, ip_address=ip_address,
    )
    note = f" It still has {dependants.describe()}." if dependants.any else ""
    return WriteResult(
        record=row, action=ChangeAction.DELETED,
        changed_fields=changed,
        message=f"{entity.label} '{code}' {verb}.{note}{followed or ''}",
    )


def restore_master(session: Session, user: UserContext, entity: ManagedEntity,
                   code: str, *, ip_address: str | None = None) -> WriteResult:
    if not entity.soft_delete:
        # Nothing to restore *to*: this entity's delete removed the row, so the
        # honest answer is that it must be created again, not un-retired.
        raise OperationRefused(
            f"{entity.label} records are removed rather than retired, so there "
            "is nothing to restore. Create the record again."
        )
    record = load_master(session, user, entity, code)
    if not getattr(record, "is_deleted", False):
        raise OperationRefused(f"{entity.label} '{code}' is not retired.")

    record.is_deleted = False
    record.deleted_at = None
    record.deleted_by = None
    session.flush()

    history.record(
        session, entity=entity, record_key=code, label=_label_of(entity, record),
        action=ChangeAction.RESTORED, user=user, ip_address=ip_address,
    )
    return WriteResult(
        record=_as_row(entity, record), action=ChangeAction.RESTORED,
        changed_fields=["is_deleted"],
        message=f"{entity.label} '{code}' restored.",
    )


def set_status(session: Session, user: UserContext, entity: ManagedEntity,
               code: str, status: str, *, ip_address: str | None = None,
               ) -> WriteResult:
    """Activate or deactivate, for the entities that model a status.

    Distinct from retiring. A deactivated customer is one you have stopped
    trading with; a retired one is a record that should not have been there.
    Both are reversible, and neither destroys anything.
    """
    if entity.status_field is None:
        raise OperationRefused(f"{entity.label} has no status to set.")
    if status.strip().lower() not in {v.lower() for v in STATUS_VALUES}:
        raise OperationRefused(
            f"Status must be one of {', '.join(STATUS_VALUES)}."
        )

    record = load_master(session, user, entity, code)
    before = {entity.status_field: getattr(record, entity.status_field, None)}
    after = {entity.status_field: status}
    change = history.diff(before, after)
    if change.is_empty:
        return WriteResult(
            record=_as_row(entity, record), action=ChangeAction.UPDATED,
            changed_fields=[], message=f"{entity.label} '{code}' is already {status}.",
        )

    setattr(record, entity.status_field, status)
    session.flush()
    history.record(
        session, entity=entity, record_key=code, label=_label_of(entity, record),
        action=ChangeAction.UPDATED, user=user, change=change,
        ip_address=ip_address,
    )
    return WriteResult(
        record=_as_row(entity, record), action=ChangeAction.UPDATED,
        changed_fields=change.fields,
        message=f"{entity.label} '{code}' set to {status}.",
    )


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


def load_transaction(session: Session, user: UserContext,
                     entity: ManagedEntity, record_id: int) -> Any:
    """Fetch one fact row, enforcing the data scope.

    Scope is checked through the row's own organisational codes rather than a
    dimension lookup: a fact carries every level it resolved to, so the deepest
    one it has is the most precise thing to test.
    """
    record = get_transaction_record(session, entity, record_id)
    if record is None:
        raise RecordNotFound(f"No {entity.label} record with id {record_id}.")
    if not _transaction_in_scope(session, user, record):
        raise ScopeDenied(
            f"You don't have permission to access this {entity.label} record. "
            f"Your access covers {user.describe_scope()}."
        )
    return record


def _transaction_in_scope(session: Session, user: UserContext,
                          record: Any) -> bool:
    from ..ai.permission_filter import PermissionFilter
    from ..org.hierarchy import ORG_CHAIN
    from ..security.scope import ORG
    from ..upload.registry import MASTER_MODEL_BY_TABLE  # noqa: F401

    if user.is_unrestricted:
        return True
    if not user.data_scope:
        return False

    permissions = PermissionFilter(session, user)
    # Every level below is organisational, so an account scoped only in another
    # dimension has no claim on these records at all. Asked explicitly because
    # ``is_within_scope`` *abstains* where a scope says nothing — the right
    # answer to "does their scope forbid this code", and the wrong one for a
    # write gate, where abstention would read as consent and hand a
    # plant-scoped account every master record in the country.
    if not permissions.has_scope_in(ORG):
        return False
    # Deepest first: the narrowest level the row resolved to is the one that
    # decides, and it implies every level above it.
    for level in reversed(ORG_CHAIN):
        surrogate = getattr(record, f"{_id_attr(level)}", None)
        if surrogate is None:
            continue
        code = _code_for_surrogate(session, level, surrogate)
        if code is None:
            continue
        return permissions.is_within_scope(f"{level}_code", code)
    return False


def _id_attr(level: str) -> str:
    """``bu`` -> ``business_unit_id``; every other level is ``<level>_id``."""
    return "business_unit_id" if level == "bu" else f"{level}_id"


def _code_for_surrogate(session: Session, level: str, surrogate: int) -> str | None:
    from ..etl.mapping import BINDING_BY_LEVEL

    binding = BINDING_BY_LEVEL.get(f"{level}_code")
    if binding is None:
        return None
    record = session.get(binding.model, surrogate)
    return getattr(record, binding.code_field, None) if record else None


def update_transaction(session: Session, user: UserContext,
                       entity: ManagedEntity, record_id: int,
                       values: dict[str, Any], *, reason: str | None = None,
                       ip_address: str | None = None) -> WriteResult:
    """Correct the measures of one transaction.

    Only the measures. Everything that places the transaction — its date, its
    customer, its organisational keys — was derived by the validated pipeline
    from the source row, and changing it here would produce a record the
    pipeline could never reproduce and the next import would overwrite.
    """
    record = load_transaction(session, user, entity, record_id)
    if getattr(record, "is_void", False):
        raise OperationRefused(
            "This transaction is voided. Restore it before correcting it."
        )

    cleaned = clean_and_validate(session, entity, values, creating=False,
                                 existing=record)
    before = {name: getattr(record, name, None) for name in cleaned}
    change = history.diff(before, cleaned)
    if change.is_empty:
        return WriteResult(
            record=transaction_row(entity, record), action=ChangeAction.UPDATED,
            changed_fields=[], message="No changes to save.",
        )

    for name in change.fields:
        setattr(record, name, cleaned[name])
    _recompute(entity, record)
    session.flush()

    history.record(
        session, entity=entity, record_key=str(record_id),
        label=getattr(record, "invoice_no", None), action=ChangeAction.UPDATED,
        user=user, change=change, reason=reason, ip_address=ip_address,
    )
    return WriteResult(
        record=transaction_row(entity, record), action=ChangeAction.UPDATED,
        changed_fields=change.fields,
        message=f"{entity.label} record {record_id} corrected.",
    )


def _recompute(entity: ManagedEntity, record: Any) -> None:
    """Re-derive the measures that are computed from others.

    Gross profit is ``net_sales - cost``, so correcting either input has to
    update it — otherwise the row would report a margin its own numbers
    contradict. The ETL's own transform is called rather than the subtraction
    repeated, so a stored sale and a corrected one are computed identically.
    """
    if entity.key != "sales":
        return
    from ..etl.transforms import build_sales_measures

    measures = build_sales_measures({
        "quantity": record.quantity,
        "gross_sales": record.gross_sales,
        "discount": record.discount,
        "net_sales": record.net_sales,
        "cost": record.cost,
    })
    record.gross_profit = measures["gross_profit"]


def void_transaction(session: Session, user: UserContext,
                     entity: ManagedEntity, record_id: int, *, reason: str,
                     ip_address: str | None = None) -> WriteResult:
    """Reverse one transaction, if nothing depends on it."""
    record = load_transaction(session, user, entity, record_id)
    if getattr(record, "is_void", False):
        raise OperationRefused("This transaction is already voided.")

    dependants = dependencies.for_transaction(session, entity, record)
    if dependants.blocking:
        raise OperationRefused(
            dependencies.CANNOT_DELETE.format(what=dependants.describe()),
            dependants=dependants,
        )

    record.is_void = True
    record.voided_at = history.now()
    record.voided_by = user.username
    record.void_reason = reason
    session.flush()

    history.record(
        session, entity=entity, record_key=str(record_id),
        label=getattr(record, "invoice_no", None), action=ChangeAction.VOIDED,
        user=user,
        change=history.Change(ChangeAction.VOIDED,
                              _transaction_measures(entity, record), {}),
        reason=reason, ip_address=ip_address,
    )
    return WriteResult(
        record=transaction_row(entity, record), action=ChangeAction.VOIDED,
        changed_fields=["is_void"],
        message=(
            f"{entity.label} record {record_id} voided. It no longer appears in "
            "any report."
        ),
    )


def unvoid_transaction(session: Session, user: UserContext,
                       entity: ManagedEntity, record_id: int, *,
                       reason: str | None = None,
                       ip_address: str | None = None) -> WriteResult:
    """Reinstate a voided transaction."""
    record = load_transaction(session, user, entity, record_id)
    if not getattr(record, "is_void", False):
        raise OperationRefused("This transaction is not voided.")

    record.is_void = False
    record.voided_at = None
    record.voided_by = None
    record.void_reason = None
    session.flush()

    history.record(
        session, entity=entity, record_key=str(record_id),
        label=getattr(record, "invoice_no", None), action=ChangeAction.UNVOIDED,
        user=user, reason=reason, ip_address=ip_address,
    )
    return WriteResult(
        record=transaction_row(entity, record), action=ChangeAction.UNVOIDED,
        changed_fields=["is_void"],
        message=f"{entity.label} record {record_id} reinstated.",
    )


def _transaction_measures(entity: ManagedEntity, record: Any) -> dict[str, Any]:
    """The figures a void reverses, kept so the history says what was undone."""
    from ..ai import queries as q

    return {
        f.name: q.normalize_value(getattr(record, f.name, None))
        for f in entity.fields
        if f.editable and hasattr(record, f.name)
    }


def transaction_row(entity: ManagedEntity, record: Any) -> dict[str, Any]:
    from ..ai import queries as q

    row = {
        f.name: q.normalize_value(getattr(record, f.name, None))
        for f in entity.fields if hasattr(record, f.name)
    }
    row[entity.id_field or "id"] = getattr(record, entity.id_field or "", None)
    row["is_void"] = bool(getattr(record, "is_void", False))
    row["void_reason"] = getattr(record, "void_reason", None)
    row["voided_by"] = getattr(record, "voided_by", None)
    row["voided_at"] = getattr(record, "voided_at", None)
    row["_key"] = str(getattr(record, entity.id_field or "", ""))
    return row


# ---------------------------------------------------------------------------
# Bulk
# ---------------------------------------------------------------------------

BULK_ACTIVATE = "ACTIVATE"
BULK_DEACTIVATE = "DEACTIVATE"
BULK_DELETE = "DELETE"
BULK_RESTORE = "RESTORE"
BULK_ACTIONS = (BULK_ACTIVATE, BULK_DEACTIVATE, BULK_DELETE, BULK_RESTORE)

#: A bulk operation is a convenience, not a way to rewrite the master data in
#: one request. Anything larger is a data load, and belongs in the upload centre
#: where it gets a preview, a validation report and a rollback.
BULK_LIMIT = 200


@dataclass
class BulkOutcome:
    succeeded: list[str]
    failed: list[dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "succeeded": self.succeeded,
            "failed": self.failed,
            "success_count": len(self.succeeded),
            "failure_count": len(self.failed),
        }


def bulk_master(session: Session, user: UserContext, entity: ManagedEntity,
                action: str, codes: list[str], *, reason: str | None = None,
                ip_address: str | None = None) -> BulkOutcome:
    """Apply one action to several master records.

    Each record is attempted independently and its outcome reported, rather than
    the whole batch failing because one code was out of scope. Partial success
    is the honest result of a partial request, and hiding it behind an
    all-or-nothing error would leave the operator guessing which rows applied.
    """
    if action not in BULK_ACTIONS:
        raise OperationRefused(
            f"Unknown bulk action '{action}'. Available: {', '.join(BULK_ACTIONS)}."
        )
    if len(codes) > BULK_LIMIT:
        raise OperationRefused(
            f"{len(codes)} records selected; {BULK_LIMIT} is the most one bulk "
            "operation may change. Use the Data Upload Center for larger changes."
        )

    succeeded: list[str] = []
    failed: list[dict[str, str]] = []

    for code in codes:
        try:
            if action == BULK_DELETE:
                delete_master(session, user, entity, code, reason=reason,
                              ip_address=ip_address)
            elif action == BULK_RESTORE:
                restore_master(session, user, entity, code,
                               ip_address=ip_address)
            else:
                set_status(session, user, entity, code,
                           "Active" if action == BULK_ACTIVATE else "Inactive",
                           ip_address=ip_address)
            succeeded.append(code)
        except (RecordNotFound, ScopeDenied, OperationRefused,
                ValidationFailed) as exc:
            failed.append({"code": code, "reason": _reason_of(exc)})

    return BulkOutcome(succeeded=succeeded, failed=failed)


def _reason_of(exc: Exception) -> str:
    if isinstance(exc, ValidationFailed):
        return "; ".join(issue.message for issue in exc.issues)
    return str(exc)


__all__ = [
    "RecordNotFound",
    "ScopeDenied",
    "OperationRefused",
    "WriteResult",
    "BulkOutcome",
    "BULK_ACTIONS",
    "BULK_ACTIVATE",
    "BULK_DEACTIVATE",
    "BULK_DELETE",
    "BULK_RESTORE",
    "BULK_LIMIT",
    "load_master",
    "create_master",
    "update_master",
    "delete_master",
    "restore_master",
    "set_status",
    "load_transaction",
    "update_transaction",
    "void_transaction",
    "unvoid_transaction",
    "bulk_master",
    "transaction_row",
]
