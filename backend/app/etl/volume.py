"""Volume: the total the source file supplied, stored exactly as supplied.

**The uploaded Total Volume is the volume.** It arrives on the transaction line
next to the quantity and the net sales figure, and it is stored unchanged. This
module does not calculate it, does not convert it, and does not fill it in from
anywhere when it is absent.

There is deliberately no unit of measure and no conversion factor in this path.
An earlier design derived the figure as ``quantity x the SKU master's pack size``
and carried the pack unit alongside it, which made every volume in the warehouse
depend on master data the source system never consulted when it raised the
invoice. A recomputed figure disagrees with the document the customer was
actually sent, so the recomputation is gone: the file states its own total and
that number is what every report adds up.

Two properties matter, and both follow from that:

**It reports rather than guesses.** A line with no volume stores NULL and is
counted as ``VOLUME_MISSING``. The sale still loads — its net sales figure is
real and rejecting the row over a missing measurement would discard revenue —
but nothing is derived to fill the hole, because a derived number is
indistinguishable from one the source actually sent.

**History is stable.** Nothing here rewrites a stored volume. Because the value
came from the file rather than from a lookup, a later correction to a master
record cannot move last July's figures: there is no expression left to
re-evaluate.

**Target volume follows the same rule since revision 0022.** It used to carry the
pack unit of the SKU it named, read through the Product Master; with that master
removed the Material Master states no unit of measure, so a planned volume is the
number the planner typed and is reported as that. There is no pack-size parsing
left in this module and no unit anywhere in the volume path.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

#: Why the supplied volume is unusable. Counted in the data-quality report so
#: that an absent figure is always attributable to the file rather than to
#: something the importer decided.
MISSING_VOLUME = "MISSING_VOLUME"
NEGATIVE_VOLUME = "NEGATIVE_VOLUME"

REASON_TEXT: dict[str, str] = {
    MISSING_VOLUME: "the source supplied no total volume",
    NEGATIVE_VOLUME: "the supplied total volume is negative",
}

MISSING_MESSAGE = "Volume Missing: {reason}."


@dataclass(frozen=True)
class VolumeResult:
    """The volume to store, and the reason when there is none.

    ``volume`` is the number the file supplied, untouched — not rounded, not
    scaled, not converted. ``reason`` explains an absent value and is what the
    data-quality report counts.
    """

    volume: Decimal | None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.volume is not None

    @property
    def message(self) -> str | None:
        if self.ok or self.reason is None:
            return None
        return MISSING_MESSAGE.format(reason=REASON_TEXT.get(self.reason,
                                                             self.reason))


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - an unparseable measure is simply absent
        return None


def from_source(volume: Any, quantity: Any) -> VolumeResult:
    """The volume to store for one line: the total the source supplied.

    The rules, in the order they are applied:

    * No volume supplied -> NULL, reason ``MISSING_VOLUME``. Nothing is derived
      to fill the hole; in particular the quantity is never multiplied by
      anything.
    * A negative volume against a non-negative quantity -> rejected upstream via
      reason ``NEGATIVE_VOLUME``. A return sends both negative and is fine: the
      sign is a property of the transaction, not of the measurement.
    * Otherwise the value is stored exactly as supplied.

    ``quantity`` is read for the sign check alone. It is never an input to the
    figure — that is the whole point of this module.
    """
    amount = _decimal(volume)
    if amount is None:
        return VolumeResult(None, MISSING_VOLUME)

    quantity_value = _decimal(quantity)
    if amount < 0 and not (quantity_value is not None and quantity_value < 0):
        return VolumeResult(None, NEGATIVE_VOLUME)

    return VolumeResult(amount)


__all__ = [
    "VolumeResult",
    "from_source",
    "MISSING_VOLUME",
    "NEGATIVE_VOLUME",
    "REASON_TEXT",
    "MISSING_MESSAGE",
]
