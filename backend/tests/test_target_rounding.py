"""Largest-remainder distribution: children that sum to their parent exactly.

Every split in the allocation engine goes through :func:`rounding.distribute`,
so a defect here is a defect in every figure the engine produces. The property
under test is a single one and it is absolute:

    ``sum(distribute(total, weights).shares) == quantise(total)``

for every input the function accepts. There is **no tolerance**. A tolerance
would hide exactly the bug this exists to prevent — a distribution that loses a
unit sits inside any epsilon anyone would pick, and the loss compounds each time
an allocation is re-run.

The cases below are the ones the specification called for — integers, decimals,
very small totals, very large totals, many children, uneven weights and zero —
plus the ones that break naive implementations: all-zero weights, a total
smaller than the number of children, and negative weights.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.targetmgmt import rounding

FOUR = Decimal("0.0001")


def total_of(shares) -> Decimal:
    return sum(shares, Decimal(0))


def quantised(value) -> Decimal:
    return Decimal(str(value)).quantize(FOUR)


# ---------------------------------------------------------------------------
# The property, over the cases the specification named
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,total,weights",
    [
        ("the specification's own example", 100, [1, 1, 1]),
        ("integer quantities", 1000, [50, 30, 20]),
        ("decimal volumes", "1234.5678", [1, 3]),
        ("decimal volume, decimal weights", "999.9999", ["1.5", "2.25", "0.75"]),
        ("very small target", "0.0003", [1, 1, 1]),
        ("target smaller than the child count", "0.0001", [1, 1, 1, 1, 1]),
        ("large target", 987654321, [7, 11, 13]),
        ("very large target, many children", 10_000_000, list(range(1, 51))),
        ("uneven allocation", 150000, [50200, 29100, 33000, 19700]),
        ("wildly uneven", 1000, [1, 1, 1, 99997]),
        ("zero allocation", 0, [1, 2, 3]),
        ("one child takes nothing", 100, [1, 0, 1]),
        ("every weight zero", 100, [0, 0, 0]),
        ("a negative weight", 100, [1, -5, 1]),
        ("one child", "77.7777", [3]),
    ],
)
def test_the_shares_always_sum_to_the_total(label, total, weights) -> None:
    result = rounding.distribute(total, weights)
    assert total_of(result.shares) == quantised(total), label
    assert len(result.shares) == len(weights)


@pytest.mark.parametrize("children", [2, 3, 7, 100, 1000])
def test_many_children_still_sum_exactly(children) -> None:
    """A thousand customers is a realistic sub-territory fan-out.

    Rounding each share independently drifts in proportion to the child count,
    which is precisely why this is the case that matters most.
    """
    result = rounding.distribute("100000.0000", [1] * children)
    assert total_of(result.shares) == Decimal("100000.0000")


def test_a_prime_split_distributes_the_remainder_rather_than_losing_it() -> None:
    """100/3 cannot be represented, so somebody must get the extra unit."""
    result = rounding.distribute(100, [1, 1, 1])
    assert total_of(result.shares) == Decimal("100.0000")
    assert sorted(str(s) for s in result.shares) == [
        "33.3333", "33.3333", "33.3334",
    ]


# ---------------------------------------------------------------------------
# Behaviour, not just arithmetic
# ---------------------------------------------------------------------------


def test_shares_follow_the_weights() -> None:
    result = rounding.distribute(1000, [50, 30, 20])
    assert [str(s) for s in result.shares] == ["500.0000", "300.0000", "200.0000"]


def test_a_zero_weight_receives_nothing() -> None:
    """Not a rounding crumb. A node the factors scored at zero gets zero."""
    result = rounding.distribute(100, [1, 0, 1])
    assert result.shares[1] == Decimal("0.0000")


def test_a_negative_weight_is_treated_as_zero() -> None:
    """A node cannot contribute negatively to a share of a positive target.

    Letting it would hand another node more than the parent has, which breaks
    the one invariant this module exists to keep.
    """
    result = rounding.distribute(100, [1, -5, 1])
    assert result.shares[1] == Decimal("0.0000")
    assert total_of(result.shares) == Decimal("100.0000")


def test_all_zero_weights_split_evenly_and_say_so() -> None:
    """Exact either way, but only one of them says anything about the business.

    The flag is what lets the screen mark a node that was split evenly for want
    of history rather than presenting it as a considered allocation.
    """
    result = rounding.distribute(100, [0, 0, 0])
    assert result.equal_fallback is True
    assert total_of(result.shares) == Decimal("100.0000")


def test_real_weights_are_not_flagged_as_a_fallback() -> None:
    assert rounding.distribute(100, [1, 2]).equal_fallback is False


def test_zero_total_gives_every_child_zero() -> None:
    result = rounding.distribute(0, [5, 3, 2])
    assert all(share == Decimal("0.0000") for share in result.shares)
    assert total_of(result.shares) == Decimal(0)


def test_no_children_is_not_an_error() -> None:
    """A leaf has nothing to distribute to, and that is an ordinary case."""
    result = rounding.distribute(100, [])
    assert result.shares == ()


def test_a_negative_total_is_refused() -> None:
    """A target to sell minus four hundred litres is a typing mistake."""
    with pytest.raises(ValueError):
        rounding.distribute(-100, [1, 1])


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_the_same_input_always_gives_the_same_output() -> None:
    """An allocation has to be reproducible.

    If two runs over identical weights handed the leftover units to different
    children, the same plan generated twice would be two different plans and
    neither could be defended.
    """
    weights = [3, 3, 3, 3, 3, 3, 3]
    first = rounding.distribute("100.0000", weights).shares
    for _ in range(20):
        assert rounding.distribute("100.0000", weights).shares == first


def test_the_map_form_is_stable_regardless_of_key_order() -> None:
    """Dictionary order is insertion order, which is stable but not meaningful.

    Two callers building the same weights in different orders must produce the
    same allocation, so the keys are sorted before distributing.
    """
    forward, _ = rounding.distribute_map(100, {"a": 1, "b": 1, "c": 1})
    backward, _ = rounding.distribute_map(100, {"c": 1, "b": 1, "a": 1})
    assert forward == backward


def test_the_map_form_sums_exactly() -> None:
    shares, _ = rounding.distribute_map("55555.5555",
                                        {f"n{i}": i + 1 for i in range(37)})
    assert total_of(shares.values()) == Decimal("55555.5555")


# ---------------------------------------------------------------------------
# Precision
# ---------------------------------------------------------------------------


def test_floats_are_read_as_written_not_as_binary() -> None:
    """``Decimal(0.1)`` is 0.1000000000000000055511151231257827.

    Volumes arrive from the database and from JSON as floats, so the conversion
    goes through ``str`` — otherwise two thousand customer shares accumulate the
    binary expansion into something visible.
    """
    result = rounding.distribute(0.3, [1, 1, 1])
    assert total_of(result.shares) == Decimal("0.3000")


def test_shares_are_quantised_to_the_stored_scale() -> None:
    """Distributing to more places than ``NUMERIC(18, 4)`` holds would reconcile
    in memory and then fail once stored."""
    result = rounding.distribute(1, [1, 1, 1])
    assert all(-share.as_tuple().exponent == rounding.VOLUME_PLACES
               for share in result.shares)


def test_the_scale_is_configurable_and_still_exact() -> None:
    result = rounding.distribute(100, [1, 1, 1], places=2)
    assert total_of(result.shares) == Decimal("100.00")
    assert sorted(str(s) for s in result.shares) == ["33.33", "33.33", "33.34"]
