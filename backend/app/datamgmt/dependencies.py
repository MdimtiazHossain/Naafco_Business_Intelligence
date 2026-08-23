"""What depends on a record, and therefore what may not be removed.

Two different questions, answered the same way.

**A transaction.** Nothing currently depends on one. Voiding an invoice used to
be refused while a collection settled it or an outstanding line tracked what was
owed on it; both datasets left the platform in revision 0020, and the streams
that remain — material stock and target — are periodic statements that settle
against nothing. ``for_transaction`` still runs on every void, and says so.

**A master record.** Retiring a customer is safe by construction — the soft
delete keeps every fact resolvable — so it is never refused. But the person
doing it should know the scale of what they are retiring, so the same count is
returned as *information*: "this customer has 1,204 transactions" is the
difference between a considered decision and an accident.

Counts are ``COUNT(*)`` with a ``LIMIT``-free predicate on an indexed column, and
they run only when someone actually presses Delete, never per row of a table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from ..database.models_warehouse import (
    FactMaterialStock,
    FactSales,
    FactTarget,
)
from .catalogue import ManagedEntity

#: Fact tables that reference a customer, sales-force or material code, and the
#: column each uses. Read when reporting the scale of a master retirement.
_MASTER_REFERENCES: dict[str, tuple[tuple[Any, str], ...]] = {
    "dim_customer": (
        (FactSales, "customer_code"),
    ),
    "dim_sales_force": (
        (FactSales, "sales_force_code"),
        (FactTarget, "sales_force_code"),
    ),
    # A stock position names its material by code, so retiring one can say how
    # many positions still carry it. Plant and storage location are absent
    # deliberately: both are keyed on a *pair* of codes, and this count takes a
    # single one, so matching on the plant code alone would silently include
    # another company's plant of the same code.
    "dim_material": (
        (FactMaterialStock, "material_code"),
    ),
}

_LABELS = {
    "fact_sales": "sales transactions",
    "fact_material_stock": "material stock positions",
    "fact_target": "targets",
}


@dataclass
class Dependants:
    """What refers to a record."""

    #: ``{"sales transactions": 1204}`` — live rows only.
    counts: dict[str, int] = field(default_factory=dict)
    #: True when these dependants must prevent the operation.
    blocking: bool = False

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def any(self) -> bool:
        return self.total > 0

    def describe(self) -> str:
        return ", ".join(
            f"{count} {label}" for label, count in sorted(self.counts.items())
            if count
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "counts": self.counts,
            "total": self.total,
            "blocking": self.blocking,
            "description": self.describe(),
        }


CANNOT_DELETE = (
    "This transaction cannot be voided because dependent records exist: {what}. "
    "Void or correct those first, so the accounting stays consistent."
)


def for_transaction(session: Session, entity: ManagedEntity,
                    record: Any) -> Dependants:
    """What depends on one transaction. Nothing does, since revision 0020.

    This used to refuse to void a sales invoice while a collection settled it or
    an outstanding line still tracked what was owed on it — voiding the invoice
    underneath either would have left the books unbalanced with nothing saying
    why. Both datasets left the platform in 0020, and no remaining transaction
    settles against another: material stock and target are periodic statements,
    not documents.

    The function stays, and stays wired into the void path, because the check is
    a property of the *void operation* rather than of the modules that happened
    to need it. A future stream that settles against an invoice adds itself
    here; until then every transaction voids freely, which is now the honest
    answer rather than a check that quietly always passes.
    """
    return Dependants()


def for_master(session: Session, entity: ManagedEntity,
               code: str) -> Dependants:
    """How much history a master record carries.

    Never blocking: the soft delete is precisely what makes retiring a
    referenced record safe. The count is returned so the confirmation dialog can
    say what is at stake.
    """
    counts: dict[str, int] = {}

    for model, column in _MASTER_REFERENCES.get(entity.key, ()):
        total = session.execute(
            select(func.count()).select_from(model.__table__)
            .where(getattr(model, column) == code)
        ).scalar_one()
        if total:
            label = _LABELS.get(model.__tablename__, model.__tablename__)
            counts[label] = counts.get(label, 0) + total

    child = _child_count(session, entity, code)
    if child:
        counts["records beneath it in the hierarchy"] = child

    return Dependants(counts=counts, blocking=False)


def _child_count(session: Session, entity: ManagedEntity, code: str) -> int:
    """Direct children in the organisational hierarchy.

    Retiring a region whose areas are still live leaves those areas parented to
    something retired. That is allowed — the hierarchy still resolves — but it
    is exactly the sort of thing an operator should be told before confirming.
    """
    from ..upload.registry import MASTER_MODEL_BY_TABLE

    from .catalogue import MASTER_ENTITIES

    total = 0
    for other in MASTER_ENTITIES:
        if other.parent_table != entity.table or not other.parent_column:
            continue
        model = MASTER_MODEL_BY_TABLE.get(other.table or "")
        if model is None:
            continue
        total += session.execute(
            select(func.count()).select_from(model.__table__).where(and_(
                getattr(model, other.parent_column) == code,
                model.is_deleted.is_(False),
            ))
        ).scalar_one()
    return total


__all__ = ["Dependants", "for_transaction", "for_master", "CANNOT_DELETE"]
