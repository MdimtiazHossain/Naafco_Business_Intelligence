"""Units of measure, and the one rule that matters: dimensions never mix.

A kilogram and a litre measure different things. Converting between them needs
a density the warehouse does not have and must not invent, so this module
refuses rather than guessing — ``convert("KG", "LTR")`` raises, it does not
approximate.

That refusal is the whole point. Everything downstream — the fact rows, the
aggregations, the dashboard, the AI agent — reports volume *per unit* because
this module makes summing across dimensions impossible rather than merely
discouraged. There is no code path that returns ``500 KG + 250 LTR = 750``.

Two dimensions are configured, each with a base unit and the sub-units that
convert cleanly into it:

===========  ==========  ===============================
Dimension    Base        Also accepted
===========  ==========  ===============================
``MASS``     ``KG``      ``GM`` (1 KG = 1000 GM),
                         ``MT`` (1 MT = 1000 KG)
``VOLUME``   ``LTR``     ``ML`` (1 LTR = 1000 ML)
===========  ==========  ===============================

Adding a dimension or a unit is one entry in :data:`UNITS`. Nothing else in the
codebase names a unit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

MASS = "MASS"
VOLUME = "VOLUME"
COUNT = "COUNT"


class IncompatibleUnits(ValueError):
    """Asked to convert between two different physical dimensions."""


class UnknownUnit(ValueError):
    """A unit string that is not configured."""


@dataclass(frozen=True)
class Unit:
    """One unit, and how much of the dimension's base unit it represents."""

    code: str
    dimension: str
    #: How many base units one of this unit is. ``GM`` -> ``0.001`` of a ``KG``.
    to_base: Decimal
    label: str
    #: Spellings accepted from a source file, matched case-insensitively after
    #: punctuation and spaces are stripped.
    aliases: tuple[str, ...] = ()

    @property
    def is_base(self) -> bool:
        return self.to_base == Decimal(1)


UNITS: tuple[Unit, ...] = (
    Unit("KG", MASS, Decimal(1), "Kilogram",
         ("kg", "kgs", "kilo", "kilos", "kilogram", "kilograms", "kgm")),
    Unit("GM", MASS, Decimal("0.001"), "Gram",
         ("g", "gm", "gms", "gram", "grams", "gr")),
    # A tonne is mass, so it totals with KG and GM and never with a litre.
    # "T" is deliberately not an alias: it is also written for "tin" and
    # "tube" in this trade, and a wrong factor of 1000 is not a small error.
    Unit("MT", MASS, Decimal(1000), "Metric Tonne",
         ("mt", "ton", "tons", "tonne", "tonnes", "metricton", "metrictons",
          "metrictonne", "metrictonnes")),
    Unit("LTR", VOLUME, Decimal(1), "Litre",
         ("l", "lt", "ltr", "ltrs", "litre", "litres", "liter", "liters")),
    Unit("ML", VOLUME, Decimal("0.001"), "Millilitre",
         ("ml", "mls", "millilitre", "millilitres", "milliliter", "milliliters")),
    # A unit for things counted rather than measured — a pack of 12 pens. Its
    # own dimension, so it can never be added to a mass or a volume either.
    Unit("PCS", COUNT, Decimal(1), "Pieces",
         ("pc", "pcs", "piece", "pieces", "unit", "units", "nos", "no", "each",
          "ea")),
)

UNIT_BY_CODE: dict[str, Unit] = {unit.code: unit for unit in UNITS}

#: Every accepted spelling -> the canonical unit. Built once.
_BY_ALIAS: dict[str, Unit] = {}
for _unit in UNITS:
    _BY_ALIAS[_unit.code.lower()] = _unit
    for _alias in _unit.aliases:
        _BY_ALIAS[_alias] = _unit

#: The base unit each dimension reports in.
BASE_UNIT: dict[str, str] = {
    unit.dimension: unit.code for unit in UNITS if unit.is_base
}

DIMENSIONS: tuple[str, ...] = (MASS, VOLUME, COUNT)


def _clean(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).strip().lower())


def parse_unit(text: str | None) -> Unit | None:
    """Resolve a source spelling to a unit, or ``None`` when unrecognised.

    Returns ``None`` rather than raising, because an unrecognised unit on a
    product is a data-quality finding to report, not an exception to abort an
    import with.
    """
    if text is None:
        return None
    key = _clean(text)
    if not key:
        return None
    return _BY_ALIAS.get(key)


def canonical(text: str | None) -> str | None:
    """``" kgs "`` -> ``"KG"``. ``None`` when unrecognised."""
    unit = parse_unit(text)
    return unit.code if unit else None


def get_unit(code: str) -> Unit:
    unit = parse_unit(code)
    if unit is None:
        raise UnknownUnit(
            f"Unknown unit {code!r}. Configured: "
            f"{', '.join(sorted(UNIT_BY_CODE))}."
        )
    return unit


def dimension_of(code: str) -> str:
    return get_unit(code).dimension


def compatible(left: str, right: str) -> bool:
    """Whether two units measure the same kind of thing."""
    try:
        return dimension_of(left) == dimension_of(right)
    except UnknownUnit:
        return False


def convert(value: Decimal | float | int, from_unit: str, to_unit: str) -> Decimal:
    """Convert within one dimension. Raises across dimensions.

    Raising is deliberate and is the module's reason for existing: a silent
    approximation between mass and volume would produce a number that looks
    like an answer and is not one.
    """
    source = get_unit(from_unit)
    target = get_unit(to_unit)
    if source.dimension != target.dimension:
        raise IncompatibleUnits(
            f"{source.code} measures {source.dimension.lower()} and "
            f"{target.code} measures {target.dimension.lower()}. Converting "
            "between them would need a density, which is not recorded — the "
            "two must be reported separately."
        )
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    return amount * source.to_base / target.to_base


def to_base(value: Decimal | float | int, unit: str) -> tuple[Decimal, str]:
    """Express a measurement in its dimension's base unit.

    Used when totalling: a report that has some rows in grams and some in
    kilograms adds them as kilograms, which is legitimate — they are the same
    dimension. It never reaches across to litres.
    """
    resolved = get_unit(unit)
    base = BASE_UNIT[resolved.dimension]
    return (convert(value, resolved.code, base), base)


#: ``"5 KG"`` / ``"5kg"`` / ``"500 ML"`` / ``"12 pcs"``. The number may carry a
#: decimal point or a thousands separator; the unit follows it.
_SIZE_PATTERN = re.compile(
    r"^\s*(?P<value>[0-9][0-9,]*(?:\.[0-9]+)?)\s*(?P<unit>[A-Za-z]+)\s*$"
)


def parse_pack_size(text: str | None) -> tuple[Decimal, str] | None:
    """``"5 KG"`` -> ``(Decimal("5"), "KG")``. ``None`` when it does not parse.

    Deliberately strict: one number followed by one recognised unit, nothing
    else. ``"5 KG bag"``, ``"5x1KG"`` and ``"approx 5kg"`` all return ``None``
    and are reported for correction, because each could mean more than one
    thing and a pack size guessed wrong silently multiplies every volume figure
    that product appears in.
    """
    if not text:
        return None
    match = _SIZE_PATTERN.match(str(text))
    if not match:
        return None
    unit = parse_unit(match.group("unit"))
    if unit is None:
        return None
    try:
        value = Decimal(match.group("value").replace(",", ""))
    except Exception:  # noqa: BLE001 - a malformed number is simply unparseable
        return None
    if value <= 0:
        return None
    return (value, unit.code)


def group_by_dimension(entries: Iterable[tuple[Decimal | float, str]],
                       ) -> dict[str, tuple[Decimal, str]]:
    """Total a mixed set of measurements, one total per dimension.

    The function every report and the AI agent use instead of ``sum()``. Given
    500 KG, 200 GM and 250 LTR it answers ``{MASS: (500.2, "KG"), VOLUME:
    (250, "LTR")}`` — two numbers, each meaningful, and no third number
    pretending they combine.
    """
    totals: dict[str, Decimal] = {}
    for value, unit in entries:
        resolved = parse_unit(unit)
        if resolved is None or value is None:
            continue
        amount, base = to_base(value, resolved.code)
        totals[base] = totals.get(base, Decimal(0)) + amount
    return {
        dimension_of(base): (total, base) for base, total in totals.items()
    }


def catalogue() -> list[dict[str, object]]:
    """The configured units, as the API and the unit filter render them."""
    return [
        {
            "code": unit.code,
            "label": unit.label,
            "dimension": unit.dimension,
            "base_unit": BASE_UNIT[unit.dimension],
            "to_base": float(unit.to_base),
            "is_base": unit.is_base,
        }
        for unit in UNITS
    ]


__all__ = [
    "MASS",
    "VOLUME",
    "COUNT",
    "DIMENSIONS",
    "UNITS",
    "UNIT_BY_CODE",
    "BASE_UNIT",
    "Unit",
    "IncompatibleUnits",
    "UnknownUnit",
    "parse_unit",
    "canonical",
    "get_unit",
    "dimension_of",
    "compatible",
    "convert",
    "to_base",
    "parse_pack_size",
    "group_by_dimension",
    "catalogue",
]
