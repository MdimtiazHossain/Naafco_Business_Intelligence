"""The country target: the one figure in this module a person types.

A country target is a Target Volume per material, for one version of one plan.
Everything downstream — the monthly split, the allocation down the hierarchy,
the review tree — is generated from these numbers, and nothing else here is
entered by hand.

**Quantity and value are derived, and derived on every read.**
``quantity = target_volume / conversion_factor`` and
``value = quantity * transfer_price``, both read from ``dim_material``. Neither
is stored, for the reason ``customer_name`` lives on a view rather than on a
fact: a corrected transfer price should correct every unlocked report at once.
What stops that from rewriting an *approved* target is that locking a version
writes it into ``fact_target``, where the figures freeze.

**A material with no conversion factor or no transfer price yields no quantity
and no value, and the total is suppressed.** This is the sharpest edge in the
module, so it is worth being plain about. Revision 0027 added both columns
nullable with no back-fill, because neither can be derived from anything else
this schema holds and a default of 1.0 would be a claim about the goods. So
until a Material Master file states them, a line derives nothing — and a
*total* over a mixture of derivable and non-derivable lines is not a small
number, it is a **wrong** one, because it silently omits whatever it could not
compute.

:func:`totals` therefore returns ``None`` for quantity and value unless every
line in scope derived, and names the materials that stopped it. A reader gets
``n/a`` and a list to act on, rather than a plausible figure that is short by an
unknown amount. Volume is always real: it is what was typed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.permission_filter import UserContext
from ..database.models import DimMaterial
from ..database.models_target import TargetCountryLine, TargetPlan, TargetVersion
from . import audit as target_audit
from .errors import TargetManagementError
from .plans import assert_editable

#: A target volume is a quantity, never a code, and never negative: a plan that
#: asks a territory to sell minus four hundred litres is not a target, it is a
#: typing mistake. Zero is allowed and meaningful — "we are not selling this
#: material here" is a real instruction, and distinct from having no line at all.
MIN_VOLUME = 0


class UnknownMaterial(TargetManagementError):
    """A material code the Material Master does not have."""

    code = "TARGET_MATERIAL_UNKNOWN"

    def __init__(self, material_code: str) -> None:
        super().__init__(
            f"material {material_code} not in the Material Master",
            user_message=(
                f"Material {material_code} is not in the Material Master. Load or "
                f"correct the master before setting a target against it."
            ),
            details={"material_code": material_code},
        )


class MaterialOutsideCompany(TargetManagementError):
    """A material belonging to a company other than the plan's.

    Refused rather than tolerated: a plan fixes one company, and a report
    filtered to that company must not carry a target for goods it does not deal
    in. ``dim_material.company_code`` is the master's own answer to which
    company a material belongs to, so this needs no guess either way.
    """

    code = "TARGET_MATERIAL_OUTSIDE_COMPANY"

    def __init__(self, material_code: str, material_company: str | None,
                 plan_company: str) -> None:
        belongs = (f"belongs to company {material_company}"
                   if material_company else "names no company")
        super().__init__(
            f"material {material_code} outside company {plan_company}",
            user_message=(
                f"Material {material_code} {belongs}, and this plan is for "
                f"company {plan_company}."
            ),
            details={"material_code": material_code,
                     "plan_company": plan_company},
        )


class InvalidVolume(TargetManagementError):
    """A target volume that is negative or not a number.

    Two failures, two messages. They send a reader to different places: an
    unreadable value means look at what was typed, a negative one means the
    number was understood and is not a target. One message covering both would
    tell somebody who typed ``12,5OO`` that it "must be zero or more", which is
    true of a value nobody managed to read and is no help at all.
    """

    code = "TARGET_VOLUME_INVALID"

    def __init__(self, material_code: str, raw: Any, *,
                 negative: bool = False) -> None:
        if negative:
            message = (
                f"Target Volume for {material_code} must be zero or more. A zero "
                f"target is a real instruction; a negative one is not."
            )
        else:
            message = (
                f"Target Volume for {material_code} is not a number: {raw!r}. It "
                f"is never coerced — correct the value rather than letting it be "
                f"read as zero."
            )
        super().__init__(
            f"invalid target volume {raw!r} for {material_code}",
            user_message=message,
            details={"material_code": material_code},
        )


@dataclass(frozen=True)
class CountryLine:
    """One material's country target, with everything needed to draw its row."""

    material_code: str
    material_description: str | None
    material_brand: str | None
    material_group_name: str | None
    company_code: str | None
    conversion_factor: float | None
    transfer_price: float | None
    target_volume: float
    quantity: float | None
    value: float | None

    @property
    def derivable(self) -> bool:
        return self.quantity is not None and self.value is not None

    @property
    def missing(self) -> list[str]:
        """Which derivation inputs the Material Master does not state.

        A property rather than something each caller recomputes: the row, the
        total and the note all have to agree on why a figure is absent, and
        three copies of that rule would eventually disagree.
        """
        return _missing_inputs(self.conversion_factor, self.transfer_price)

    def to_dict(self) -> dict[str, Any]:
        return {
            "material_code": self.material_code,
            "material_description": self.material_description,
            "material_brand": self.material_brand,
            "material_group_name": self.material_group_name,
            "company_code": self.company_code,
            "conversion_factor": self.conversion_factor,
            "transfer_price": self.transfer_price,
            "target_volume": self.target_volume,
            "quantity": self.quantity,
            "value": self.value,
            # Spelled out rather than left to the browser to infer from two
            # nulls: the reason a cell reads n/a is what the reader has to act
            # on, and "no conversion factor" and "no transfer price" send them
            # to different columns of the same master file.
            "missing": self.missing,
        }


def _missing_inputs(conversion_factor: float | None,
                    transfer_price: float | None) -> list[str]:
    missing = []
    if conversion_factor is None or float(conversion_factor) == 0:
        missing.append("conversion_factor")
    if transfer_price is None:
        missing.append("transfer_price")
    return missing


def derive(target_volume: float, conversion_factor: float | None,
           transfer_price: float | None) -> tuple[float | None, float | None]:
    """``(quantity, value)``, or ``None`` for whichever cannot be computed.

    A conversion factor of zero is treated as missing rather than as a division
    by zero: a pack that contains none of the goods is not a fact about the
    goods, it is an unfilled cell that happens to have been typed as 0.

    Value depends on quantity, so a material with a transfer price but no
    conversion factor derives neither — there is nothing to multiply.
    """
    if conversion_factor is None or float(conversion_factor) == 0:
        return None, None
    quantity = float(target_volume) / float(conversion_factor)
    if transfer_price is None:
        return quantity, None
    return quantity, quantity * float(transfer_price)


def list_lines(session: Session, version_id: int) -> list[CountryLine]:
    """Every country line on a version, with its material and derived figures.

    A LEFT JOIN, and for the same reason ``vw_sales_detail`` joins the customer
    master with one: a line whose material the master has since lost keeps its
    figures and shows its code, rather than vanishing from a total it is part of.
    """
    rows = session.execute(
        select(TargetCountryLine, DimMaterial)
        .join(DimMaterial,
              DimMaterial.material_code == TargetCountryLine.material_code,
              isouter=True)
        .where(TargetCountryLine.version_id == version_id)
        .order_by(TargetCountryLine.material_code)
    ).all()

    lines: list[CountryLine] = []
    for line, material in rows:
        conversion_factor = (
            float(material.conversion_factor)
            if material is not None and material.conversion_factor is not None
            else None
        )
        transfer_price = (
            float(material.transfer_price)
            if material is not None and material.transfer_price is not None
            else None
        )
        volume = float(line.target_volume or 0)
        quantity, value = derive(volume, conversion_factor, transfer_price)
        lines.append(CountryLine(
            material_code=line.material_code,
            material_description=material.material_description if material else None,
            material_brand=material.material_brand if material else None,
            material_group_name=material.material_group_name if material else None,
            company_code=material.company_code if material else None,
            conversion_factor=conversion_factor,
            transfer_price=transfer_price,
            target_volume=volume,
            quantity=quantity,
            value=value,
        ))
    return lines


def totals(lines: Iterable[CountryLine]) -> dict[str, Any]:
    """The country total. Volume always; quantity and value only if complete.

    See the module docstring: a total over a mixture of derivable and
    non-derivable lines omits whatever it could not compute, and a figure that
    is short by an unknown amount is worse than no figure. So the two derived
    totals are ``None`` unless every line derived, and the materials that
    stopped them are named.
    """
    rows = list(lines)
    volume = sum(line.target_volume for line in rows)

    without_factor = [line.material_code for line in rows
                      if "conversion_factor" in line.missing]
    without_price = [line.material_code for line in rows
                     if "transfer_price" in line.missing]

    complete = all(line.derivable for line in rows) and bool(rows)
    return {
        "line_count": len(rows),
        "target_volume": volume,
        "quantity": sum(line.quantity or 0 for line in rows) if complete else None,
        "value": sum(line.value or 0 for line in rows) if complete else None,
        "derivable_count": sum(1 for line in rows if line.derivable),
        "missing_conversion_factor": without_factor,
        "missing_transfer_price": without_price,
    }


def notes(summary: dict[str, Any]) -> list[str]:
    """What a reader has to know to act on an n/a, in their own words.

    Attached to the response rather than composed in the browser, so the chat
    answer, the export and the screen all explain a suppressed total the same
    way — and so the explanation is written once, next to the rule that
    produces it.
    """
    lines: list[str] = []
    without_factor = summary["missing_conversion_factor"]
    without_price = summary["missing_transfer_price"]
    if not (without_factor or without_price):
        return lines

    lines.append(
        "Quantity and Value cannot be totalled while any material is missing a "
        "Conversion Factor or a Transfer Price — a partial total would be short "
        "by an unknown amount."
    )
    if without_factor:
        lines.append(
            f"{len(without_factor)} material(s) state no Conversion Factor: "
            f"{_sample(without_factor)}. Quantity is derived as Target Volume "
            f"divided by it, so neither Quantity nor Value can be computed."
        )
    if without_price:
        lines.append(
            f"{len(without_price)} material(s) state no Transfer Price: "
            f"{_sample(without_price)}. Value is Quantity times it."
        )
    lines.append(
        "Both are columns of the Material Master upload. Nothing is defaulted: "
        "a Conversion Factor of 1.0 would be a claim about the pack, not a "
        "blank."
    )
    return lines


def _sample(codes: list[str], limit: int = 5) -> str:
    """The first few codes, and how many more. A list of 400 helps nobody."""
    shown = ", ".join(codes[:limit])
    remaining = len(codes) - limit
    return f"{shown} and {remaining} more" if remaining > 0 else shown


def available_materials(session: Session, plan: TargetPlan,
                        version_id: int) -> list[dict[str, Any]]:
    """Materials that may be added to this version's country target.

    Narrowed to the plan's own company, because the plan fixes one and
    ``dim_material.company_code`` is the master's answer to which company a
    material belongs to. Retired materials are excluded from *selection*, which
    is what ``is_deleted`` means — a line already naming one keeps it, because
    retiring a record removes it from choice, not from history.
    """
    already = {
        code for (code,) in session.execute(
            select(TargetCountryLine.material_code)
            .where(TargetCountryLine.version_id == version_id)
        )
    }
    rows = session.execute(
        select(DimMaterial)
        .where(DimMaterial.company_code == plan.company_code,
               DimMaterial.is_deleted.is_(False))
        .order_by(DimMaterial.material_brand, DimMaterial.material_code)
    ).scalars().all()
    return [
        {
            "material_code": material.material_code,
            "material_description": material.material_description,
            "material_brand": material.material_brand,
            "material_group_name": material.material_group_name,
            "conversion_factor": (float(material.conversion_factor)
                                  if material.conversion_factor is not None else None),
            "transfer_price": (float(material.transfer_price)
                               if material.transfer_price is not None else None),
        }
        for material in rows
        if material.material_code not in already
    ]


def set_lines(session: Session, user: UserContext, *, version: TargetVersion,
              plan: TargetPlan,
              entries: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    """Write country volumes. Returns what changed.

    Validation happens for **every** entry before anything is written, so a file
    or a grid with one bad row does not leave half its changes applied — the
    same all-or-nothing shape the ETL gives an import.

    Each changed line is audited individually with its old and new value,
    because that is the row the Audit Trail screen shows. A line whose value did
    not actually change writes nothing: re-saving a grid is not fourteen
    revisions of the same number.
    """
    assert_editable(version)

    requested: dict[str, float] = {}
    for material_code, raw in entries:
        code = (material_code or "").strip()
        if not code:
            raise UnknownMaterial("(blank)")
        requested[code] = _coerce_volume(code, raw)

    if not requested:
        return {"updated": 0, "created": 0}

    materials = {
        material.material_code: material
        for material in session.execute(
            select(DimMaterial).where(
                DimMaterial.material_code.in_(list(requested)))
        ).scalars()
    }
    for code in requested:
        material = materials.get(code)
        if material is None:
            raise UnknownMaterial(code)
        if material.company_code != plan.company_code:
            raise MaterialOutsideCompany(code, material.company_code,
                                         plan.company_code)

    existing = {
        line.material_code: line
        for line in session.execute(
            select(TargetCountryLine).where(
                TargetCountryLine.version_id == version.version_id,
                TargetCountryLine.material_code.in_(list(requested)))
        ).scalars()
    }

    created = 0
    updated = 0
    for code, volume in requested.items():
        line = existing.get(code)
        if line is None:
            session.add(TargetCountryLine(
                version_id=version.version_id, material_code=code,
                target_volume=volume, updated_by=user.username,
            ))
            created += 1
            target_audit.record(
                session, action=target_audit.TargetAction.COUNTRY_TARGET_EDITED,
                plan_id=plan.plan_id, version_id=version.version_id,
                actor=user.username, actor_role=user.role,
                node_label=f"Country · {code}",
                old_value=None, new_value=_format_volume(volume),
            )
            continue

        previous = float(line.target_volume or 0)
        if previous == volume:
            continue
        line.target_volume = volume
        line.updated_by = user.username
        updated += 1
        target_audit.record(
            session, action=target_audit.TargetAction.COUNTRY_TARGET_EDITED,
            plan_id=plan.plan_id, version_id=version.version_id,
            actor=user.username, actor_role=user.role,
            node_label=f"Country · {code}",
            old_value=_format_volume(previous),
            new_value=_format_volume(volume),
        )

    session.flush()
    return {"created": created, "updated": updated}


def _coerce_volume(material_code: str, raw: Any) -> float:
    """A volume, or a refusal. Never a zero standing in for something unreadable.

    The no-invented-data invariant applied to one cell: ``"12,5OO"`` — with a
    letter O in it — is rejected rather than read as 125 or as 0, exactly as the
    ETL rejects it in an uploaded file.
    """
    if raw is None or isinstance(raw, bool):
        raise InvalidVolume(material_code, raw)
    if isinstance(raw, str):
        text = raw.strip().replace(",", "")
        if not text:
            raise InvalidVolume(material_code, raw)
        try:
            value = float(Decimal(text))
        except Exception as exc:  # noqa: BLE001 - any parse failure is a refusal
            raise InvalidVolume(material_code, raw) from exc
    elif isinstance(raw, (int, float, Decimal)):
        value = float(raw)
    else:
        raise InvalidVolume(material_code, raw)

    # NaN and the infinities parse cleanly through ``Decimal`` and ``float`` but
    # are not quantities: a target of infinity is unreadable in the same way
    # ``"abc"`` is, so it takes the same message rather than the negative one.
    if value != value or value in (float("inf"), float("-inf")):
        raise InvalidVolume(material_code, raw)
    if value < MIN_VOLUME:
        raise InvalidVolume(material_code, raw, negative=True)
    return value


def _format_volume(value: float) -> str:
    """How a volume reads in the audit trail: no trailing ``.0`` on a whole one."""
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


__all__ = [
    "MIN_VOLUME",
    "UnknownMaterial",
    "MaterialOutsideCompany",
    "InvalidVolume",
    "CountryLine",
    "derive",
    "list_lines",
    "totals",
    "notes",
    "available_materials",
    "set_lines",
]
