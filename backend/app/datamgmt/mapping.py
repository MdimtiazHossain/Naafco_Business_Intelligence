"""Deriving a master link from data that already exists.

The column below describes a relationship the warehouse already knows about
somewhere. The job here is to find it where it is unambiguous and to refuse
where it is not — a wrong sub-territory on a customer is worse than a blank one,
because a blank is visibly missing and a wrong one is invisibly wrong.

**Customer → sub-territory.** ``dim_customer`` has no organisational column, but
the *facts* do: every sales row carries both a customer code and the
sub-territory the ETL resolved for it. So a customer that
has only ever traded in one sub-territory has an unambiguous answer, and that is
the only case that is written. A customer that has traded in several is a
genuine ambiguity — it may be a shared dealer, or it may be a data error — and
is reported, not resolved. A customer with no transactions has no evidence at
all.

There used to be a second derivation here — product → company, confirming a SKU
master's free-text ``producer_company`` against ``dim_company``. Revision 0022
removed that master, and the Material Master which replaced it states no
producing company at all, so there is no text left to confirm and none is
invented. ``normalise_company`` survives because the quality report still
compares company names.

Nothing here writes a value it cannot justify from an existing row, and nothing
overwrites a value someone has already set.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database.models import DimCompany, DimMaterial, DimSubTerritory
from ..database.models_warehouse import DimCustomer, FactSales

#: Fact tables carrying both a customer code and a resolved sub-territory.
#: Sales alone since revision 0020 — it is the only transactional stream left
#: that names a customer.
_CUSTOMER_FACTS = (FactSales,)

#: Suffixes stripped before comparing a company name. Matching "NAAFCO Ltd."
#: to "NAAFCO" is not a guess — it is the same company written two ways — while
#: matching "NAAFCO" to "NAAFCO Agrovet" would be, and is not done: the
#: comparison is equality after normalisation, never a prefix or a substring.
_COMPANY_SUFFIXES = (
    "limited", "ltd", "pvt", "private", "plc", "inc", "incorporated",
    "company", "co", "corporation", "corp", "llc", "and company",
)


def normalise_company(name: str) -> str:
    """A company name reduced to what is comparable about it."""
    text = unicodedata.normalize("NFKD", str(name)).lower()
    text = re.sub(r"[^\w\s]", " ", text)
    words = [w for w in text.split() if w]
    while words and words[-1] in _COMPANY_SUFFIXES:
        words.pop()
    return " ".join(words)


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------

MAPPED = "mapped"
UNMAPPED_NO_EVIDENCE = "no_evidence"
UNMAPPED_AMBIGUOUS = "ambiguous"
CONFLICT = "conflict"
ALREADY_SET = "already_set"


@dataclass
class RecordOutcome:
    """What was decided for one record, and why."""

    code: str
    outcome: str
    value: str | None = None
    reason: str = ""
    candidates: list[str] = field(default_factory=list)


@dataclass
class MappingReport:
    """The result of one mapping pass over one master."""

    entity: str
    field_name: str
    total: int = 0
    already_set: int = 0
    mapped: list[RecordOutcome] = field(default_factory=list)
    unmapped: list[RecordOutcome] = field(default_factory=list)
    conflicts: list[RecordOutcome] = field(default_factory=list)

    @property
    def mapped_count(self) -> int:
        return len(self.mapped)

    @property
    def unmapped_count(self) -> int:
        return len(self.unmapped)

    @property
    def conflict_count(self) -> int:
        return len(self.conflicts)

    def to_dict(self, samples: int = 50) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "field": self.field_name,
            "total": self.total,
            "already_set": self.already_set,
            "mapped": self.mapped_count,
            "unmapped": self.unmapped_count,
            "conflicts": self.conflict_count,
            "unmapped_records": [
                {"code": o.code, "reason": o.reason, "candidates": o.candidates}
                for o in self.unmapped[:samples]
            ],
            "conflict_records": [
                {"code": o.code, "reason": o.reason, "value": o.value,
                 "candidates": o.candidates}
                for o in self.conflicts[:samples]
            ],
        }


# ---------------------------------------------------------------------------
# Customer → sub-territory
# ---------------------------------------------------------------------------


def customer_sub_territories(session: Session) -> dict[str, set[str]]:
    """Every sub-territory each customer has transacted in, across all history.

    Over all history rather than a period, for the same reason the map derives
    membership that way: where a customer *belongs* is a standing fact, and a
    date filter must not change it.
    """
    found: dict[str, set[str]] = {}
    for model in _CUSTOMER_FACTS:
        rows = session.execute(
            select(model.customer_code, DimSubTerritory.sub_territory_code)
            .join(DimSubTerritory,
                  DimSubTerritory.sub_territory_id == model.sub_territory_id)
            .where(model.customer_code.isnot(None))
            .distinct()
        ).all()
        for customer_code, sub_territory_code in rows:
            if not customer_code or not sub_territory_code:
                continue
            found.setdefault(str(customer_code), set()).add(str(sub_territory_code))
    return found


def map_customers(session: Session, *, apply: bool = False,
                  actor: str = "mapping") -> MappingReport:
    """Derive ``dim_customer.sub_territory_code`` from the transaction history."""
    report = MappingReport(entity="dim_customer", field_name="sub_territory_code")

    valid = {
        row[0] for row in session.execute(select(DimSubTerritory.sub_territory_code))
    }
    evidence = customer_sub_territories(session)

    customers = session.execute(select(DimCustomer)).scalars().all()
    report.total = len(customers)

    for customer in customers:
        code = customer.customer_code
        current = customer.sub_territory_code
        seen = evidence.get(code, set())

        if current:
            report.already_set += 1
            # An existing value is never overwritten — but it is checked, so a
            # value contradicting every transaction is surfaced rather than
            # left to be discovered by someone reading a wrong report.
            if seen and current not in seen:
                report.conflicts.append(RecordOutcome(
                    code=code, outcome=CONFLICT, value=current,
                    reason=(
                        f"Set to {current}, but every transaction for this "
                        f"customer resolved to {', '.join(sorted(seen))}."
                    ),
                    candidates=sorted(seen),
                ))
            elif current not in valid:
                report.conflicts.append(RecordOutcome(
                    code=code, outcome=CONFLICT, value=current,
                    reason=(
                        f"Sub-territory {current} is not in the Sub-Territory "
                        "Master."
                    ),
                ))
            continue

        if not seen:
            report.unmapped.append(RecordOutcome(
                code=code, outcome=UNMAPPED_NO_EVIDENCE,
                reason="No transaction resolves this customer to a sub-territory.",
            ))
            continue

        if len(seen) > 1:
            report.unmapped.append(RecordOutcome(
                code=code, outcome=UNMAPPED_AMBIGUOUS,
                reason=(
                    "Transactions place this customer in more than one "
                    "sub-territory, so no single value is safe to write."
                ),
                candidates=sorted(seen),
            ))
            continue

        resolved = next(iter(seen))
        if resolved not in valid:
            report.unmapped.append(RecordOutcome(
                code=code, outcome=UNMAPPED_NO_EVIDENCE,
                reason=(
                    f"Transactions resolve to {resolved}, which is not in the "
                    "Sub-Territory Master."
                ),
            ))
            continue

        report.mapped.append(RecordOutcome(
            code=code, outcome=MAPPED, value=resolved,
            reason="Every transaction for this customer resolves here.",
        ))
        if apply:
            customer.sub_territory_code = resolved

    if apply:
        session.flush()
    return report


# No product → company mapping. It derived ``dim_product.company_code`` from that
# master's free-text ``producer_company``, and revision 0022 removed the master.
# The Material Master states no producing company at all, so there is no text to
# confirm against ``dim_company`` and nothing here to replace it with —
# ``normalise_company`` stays because the quality report below still compares
# company names.


# ---------------------------------------------------------------------------
# Data quality
# ---------------------------------------------------------------------------


def quality_issues(session: Session) -> dict[str, Any]:
    """Duplicate, invalid and missing codes on the customer link.

    Reported, never corrected. A duplicate code is a decision for whoever owns
    the master data; deleting one automatically would destroy the evidence
    needed to decide which.

    The ``material`` section reports the Material Master's own integrity rather
    than a link to somewhere else: a material references no other master, so
    there is no foreign code to validate — only its own that can collide.
    """
    valid_sub = {
        row[0] for row in session.execute(select(DimSubTerritory.sub_territory_code))
    }

    customer_rows = session.execute(
        select(DimCustomer.customer_code, DimCustomer.sub_territory_code)
        .where(DimCustomer.is_deleted.is_(False))
    ).all()

    return {
        "customer": {
            "missing_sub_territory": sorted(
                code for code, sub in customer_rows if not sub
            )[:200],
            "invalid_sub_territory": sorted(
                f"{code} → {sub}" for code, sub in customer_rows
                if sub and sub not in valid_sub
            )[:200],
            "duplicate_sub_territory_codes": _duplicates(
                session, DimSubTerritory, DimSubTerritory.sub_territory_code),
        },
        "material": {
            "duplicate_material_codes": _duplicates(
                session, DimMaterial, DimMaterial.material_code),
            "duplicate_company_codes": _duplicates(
                session, DimCompany, DimCompany.company_code),
        },
    }


def _duplicates(session: Session, model: Any, column: Any) -> list[str]:
    """Codes appearing more than once, case-insensitively.

    The unique constraint already prevents exact repeats, so what this finds is
    the pair that differs only by case — ``C01`` and ``c01`` — which the
    database accepts and a human reads as one code.
    """
    rows = session.execute(
        select(func.lower(column), func.count())
        .group_by(func.lower(column))
        .having(func.count() > 1)
    ).all()
    return [str(value) for value, _count in rows]


def summary(session: Session) -> dict[str, Any]:
    """The Master Data Mapping Status report, without writing anything.

    One derived link now, not two: customer → sub-territory. The product →
    company derivation left with the master it wrote to in revision 0022.
    """
    customers = map_customers(session, apply=False)
    return {
        "customer": customers.to_dict(),
        "quality": quality_issues(session),
    }


__all__ = [
    "MAPPED",
    "UNMAPPED_NO_EVIDENCE",
    "UNMAPPED_AMBIGUOUS",
    "CONFLICT",
    "RecordOutcome",
    "MappingReport",
    "normalise_company",
    "customer_sub_territories",
    "map_customers",
    "quality_issues",
    "summary",
]
