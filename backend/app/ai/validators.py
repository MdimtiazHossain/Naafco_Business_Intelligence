"""Result validation — the last gate before numbers reach the user.

Every tool result is checked here. If a check fails the answer is suppressed
rather than shown with a caveat: a wrong business number is worse than no
number. Anything merely suspicious becomes a note attached to the answer.

Checked: finite numbers, no NaN/Inf, non-negative where the measure forbids it,
percentages inside a believable band, row counts consistent with the data,
divide-by-zero handled as NULL, and the date window matching what was asked.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from .schemas import ResolvedDateRange, ToolResult

#: Measures that can never legitimately be negative in an aggregate.
NON_NEGATIVE_MEASURES = {
    "quantity_total", "gross_sales_abs", "target_amount",
    "transaction_count", "invoice_count", "row_count", "target_count",
}

#: Percentages outside this band almost always mean a broken denominator.
PERCENT_MIN = -100_000.0
PERCENT_MAX = 100_000.0

PERCENT_SUFFIXES = ("_percent", "_pct")


@dataclass
class ValidationOutcome:
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.ok = False
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_number(name: str, value: Any, outcome: ValidationOutcome) -> None:
    if value is None or not _is_number(value):
        return
    if math.isnan(value):
        outcome.fail(f"{name} is NaN")
        return
    if math.isinf(value):
        outcome.fail(f"{name} is infinite")
        return
    if name.endswith(PERCENT_SUFFIXES) and not (PERCENT_MIN <= value <= PERCENT_MAX):
        outcome.fail(f"{name} is {value}, which is outside a believable range")
    if name in NON_NEGATIVE_MEASURES and value < 0:
        outcome.fail(f"{name} is negative ({value})")


def validate_tool_result(result: ToolResult,
                         expected_range: ResolvedDateRange | None = None,
                         ) -> ValidationOutcome:
    """Validate one tool result before it is formatted."""
    outcome = ValidationOutcome()

    _check_number("value", result.value, outcome)
    for key, value in result.values.items():
        if isinstance(value, dict):
            for inner_key, inner_value in value.items():
                _check_number(f"{key}.{inner_key}", inner_value, outcome)
        else:
            _check_number(key, value, outcome)

    for index, row in enumerate(result.rows):
        if not isinstance(row, dict):
            outcome.fail(f"row {index} is not a record")
            continue
        for key, value in row.items():
            _check_number(f"row[{index}].{key}", value, outcome)

    if result.row_count < 0:
        outcome.fail("row_count is negative")
    if result.rows and result.row_count < len(result.rows):
        outcome.fail(
            f"row_count ({result.row_count}) is smaller than the number of rows "
            f"returned ({len(result.rows)})"
        )

    if result.date_from and result.date_to and result.date_to < result.date_from:
        outcome.fail("the result's date range runs backwards")

    if expected_range is not None and result.date_from and result.date_to:
        if (result.date_from != expected_range.date_from
                or result.date_to != expected_range.date_to):
            # Stock is a snapshot and legitimately ignores the window. Every
            # stock tool is named ``get_stock_*`` bar the expiry listing, which
            # reads the same dateless position.
            if result.tool.startswith("get_stock") or result.tool == "get_expiring_stock":
                outcome.warn("stock is a snapshot; the requested window does not apply")
            else:
                outcome.fail(
                    f"the result covers {result.date_from}..{result.date_to} but "
                    f"{expected_range.date_from}..{expected_range.date_to} was requested"
                )

    if result.chart is not None and not result.chart.data:
        outcome.warn("chart was produced with no data points; it will be dropped")

    if result.currency != "BDT":
        outcome.warn(f"unexpected currency {result.currency}")

    return outcome


def validate_all(results: Iterable[ToolResult],
                 expected_range: ResolvedDateRange | None = None) -> ValidationOutcome:
    combined = ValidationOutcome()
    for result in results:
        outcome = validate_tool_result(result, expected_range)
        combined.errors.extend(outcome.errors)
        combined.warnings.extend(outcome.warnings)
        combined.ok = combined.ok and outcome.ok
    return combined


def numbers_in(text: str) -> list[str]:
    """Every number-looking token in a string.

    Used to check that a generated sentence only quotes figures that appear in
    the validated tool output — the guard against a fluent but invented number.
    """
    import re

    return re.findall(r"\d[\d,]*\.?\d*", text)


def response_grounded_in(text: str, allowed: Iterable[str]) -> bool:
    """True when every number in ``text`` also appears in the tool output."""
    allowed_set = {a.replace(",", "") for a in allowed}
    for token in numbers_in(text):
        cleaned = token.replace(",", "").rstrip(".")
        if cleaned and cleaned not in allowed_set:
            return False
    return True


__all__ = [
    "ValidationOutcome",
    "validate_tool_result",
    "validate_all",
    "numbers_in",
    "response_grounded_in",
    "NON_NEGATIVE_MEASURES",
]
