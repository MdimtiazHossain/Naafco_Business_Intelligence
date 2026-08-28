"""Largest-remainder distribution: children that sum to their parent exactly.

Every split in the allocation engine passes through here, and the reason is one
sentence: **a target that does not add up is not a target.** If a country volume
of 100,000 is distributed across four regions and the four come back as 99,999.9,
the missing tenth has to live somewhere, and there is nowhere for it to live —
so the reconciliation bar goes red, final approval is blocked, and a planner is
left hunting a rounding error rather than judging a plan.

**Why largest remainder and not proportional rounding.** Rounding each share
independently is the obvious approach and it is wrong: round-half-up on
``[33.333, 33.333, 33.333]`` of 100 gives ``33.33 + 33.33 + 33.33 = 99.99``, and
round-half-even gives the same. The error is unbounded in the number of
children — a thousand customers can drift by several whole units. Largest
remainder instead floors every share, counts how many indivisible units are left
over, and hands them to the children with the largest fractional parts. The
result sums to the total by construction rather than by luck, and each child is
within one unit of its exact share.

**Everything is Decimal, and never float.** ``0.1 + 0.2 != 0.3`` in binary
floating point, and a sum of two thousand customer shares accumulates that error
into something visible. ``fact_target`` stores ``NUMERIC(18, 4)``, so the
arithmetic here is decimal from end to end and quantised to the same scale the
database will hold.

**Ties are broken deterministically.** Two children with identical fractional
parts must not receive the leftover unit in an order that depends on dictionary
iteration or on floating-point noise, or the same allocation run twice would
produce two different plans and neither could be reproduced. The order is:
largest fraction first, then largest weight, then lowest index.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Sequence

#: Decimal places every allocated volume is held to.
#:
#: Four, matching ``NUMERIC(18, 4)`` on ``target_allocation.system_volume`` and
#: on ``fact_target``. Distributing to more places than the column holds would
#: reconcile in memory and then fail once stored, which is the worst of both.
VOLUME_PLACES = 4


@dataclass(frozen=True)
class Distribution:
    """The shares, and how they were arrived at.

    ``equal_fallback`` matters to the caller: shares produced from real weights
    and shares produced because every weight was zero are both exact, but only
    one of them says anything about the business. The engine records which, so
    the screen can mark a node that was split evenly for want of history rather
    than presenting it as a considered allocation.
    """

    shares: tuple[Decimal, ...]
    equal_fallback: bool

    @property
    def total(self) -> Decimal:
        return sum(self.shares, Decimal(0))


def quantum(places: int = VOLUME_PLACES) -> Decimal:
    """The smallest indivisible unit at ``places`` decimals — ``0.0001`` at 4."""
    return Decimal(1).scaleb(-places)


def distribute(total: Decimal | float | int,
               weights: Sequence[Decimal | float | int],
               places: int = VOLUME_PLACES) -> Distribution:
    """Split ``total`` across ``weights`` so the shares sum to it exactly.

    The postcondition is the whole point and is worth stating plainly:
    ``sum(result.shares) == quantise(total)``, exactly, for every input this
    accepts. The tests assert it over integers, decimals, very small and very
    large totals, a thousand children, wildly uneven weights and weights that
    are all zero.

    ``weights`` need not sum to anything in particular — they are relative. A
    negative weight is treated as zero: a node cannot contribute negatively to
    a share of a positive target, and silently letting one do so would hand
    another node more than the parent has.
    """
    if not weights:
        return Distribution(shares=(), equal_fallback=False)

    step = quantum(places)
    amount = _decimal(total).quantize(step)
    if amount < 0:
        raise ValueError("cannot distribute a negative total")

    cleaned = [max(_decimal(weight), Decimal(0)) for weight in weights]
    weight_total = sum(cleaned, Decimal(0))

    equal_fallback = weight_total == 0
    if equal_fallback:
        # Nothing to weight by. An equal split is the only distribution that
        # does not invent a preference between the children, and the flag tells
        # the caller it was not a judgement about the business.
        cleaned = [Decimal(1)] * len(cleaned)
        weight_total = Decimal(len(cleaned))

    if amount == 0:
        # Zero distributes to zero, and does so without running the remainder
        # loop — which would otherwise hand out units of a total that is not
        # there.
        return Distribution(shares=tuple(Decimal(0).quantize(step)
                                         for _ in cleaned),
                            equal_fallback=equal_fallback)

    exact = [amount * weight / weight_total for weight in cleaned]
    floors = [value.quantize(step, rounding=ROUND_FLOOR) for value in exact]

    # How many indivisible units the flooring left unallocated. Computed in
    # units rather than in currency so it is an exact integer count.
    allocated = sum(floors, Decimal(0))
    leftover = int(((amount - allocated) / step).to_integral_value())

    if leftover > 0:
        order = sorted(
            range(len(cleaned)),
            key=lambda index: (
                -(exact[index] - floors[index]),   # largest fraction first
                -cleaned[index],                   # then the larger weight
                index,                             # then stable by position
            ),
        )
        for position in range(leftover):
            index = order[position % len(order)]
            floors[index] = floors[index] + step

    return Distribution(shares=tuple(floors), equal_fallback=equal_fallback)


def distribute_map(total: Decimal | float | int,
                   weights: dict[str, Decimal | float | int],
                   places: int = VOLUME_PLACES) -> tuple[dict[str, Decimal], bool]:
    """:func:`distribute` keyed by node code, in a stable order.

    Dictionary order is insertion order in Python, which is stable but is not
    *meaningful* — so the keys are sorted before distributing. Two runs over the
    same weights then hand the leftover units to the same nodes, and an
    allocation is reproducible.
    """
    keys = sorted(weights)
    result = distribute(total, [weights[key] for key in keys], places)
    return dict(zip(keys, result.shares)), result.equal_fallback


def _decimal(value: Decimal | float | int) -> Decimal:
    """A ``Decimal`` that means what the caller wrote.

    ``Decimal(0.1)`` is ``0.1000000000000000055511151231257827``; ``Decimal(str(
    0.1))`` is ``0.1``. Floats reach this module from the database and from
    JSON, so the conversion goes through ``str`` rather than trusting the binary
    expansion of a number nobody typed in binary.
    """
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


__all__ = [
    "VOLUME_PLACES",
    "Distribution",
    "quantum",
    "distribute",
    "distribute_map",
]
