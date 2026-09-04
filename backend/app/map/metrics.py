"""The measures a map layer can draw, declared once.

A layer names a *metric* (what a point's figure is), a *colour metric* and a
*size metric*, and every one of them has to be a key in :data:`METRICS`: a
metric key the settings screen offers but nothing can compute would draw a map
of blanks, which is the failure the agent's metric keywords guard against by
the same rule.

Every metric here is read off one row of :func:`app.ai.tools.get_map_layer` —
the ``field`` names the column of that row — so the registry states what the
map *can* draw and the tool states how it is computed, and the two are pinned
together by ``test_map_data``. **No metric is invented.** Active Customer Count
and New Customer Count are not here: no definition of "active" or "new" exists
in this warehouse, and they become rows the day somebody writes the rule down.

``unavailable_at`` names levels at which a metric is meaningless rather than
merely zero. A customer's customer count is one, always, and a size scale on
it would draw every customer the same — so the metric is refused for that
level rather than rendered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: How a figure is formatted. The frontend keys its formatters on these.
KIND_CURRENCY = "currency"
KIND_QUANTITY = "quantity"
KIND_VOLUME = "volume"
KIND_PERCENT = "percent"
KIND_COUNT = "count"


@dataclass(frozen=True)
class MapMetric:
    """One thing a point can be sized or coloured by."""

    key: str
    label: str
    #: The column of a ``get_map_layer`` row that carries the figure.
    field: str
    kind: str
    #: Whether a larger figure is the better one. Shortfall is negative when
    #: short, so larger *is* better there too; growth likewise.
    higher_is_better: bool = True
    #: A figure that may legitimately be negative — a colour scale for it needs
    #: a midpoint, not a floor.
    signed: bool = False
    #: Levels at which this metric is meaningless.
    unavailable_at: tuple[str, ...] = ()
    description: str = ""

    def available_at(self, level: str) -> bool:
        return level not in self.unavailable_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "field": self.field,
            "kind": self.kind,
            "higher_is_better": self.higher_is_better,
            "signed": self.signed,
            "unavailable_at": list(self.unavailable_at),
            "description": self.description,
        }


METRICS: tuple[MapMetric, ...] = (
    MapMetric("net_sales", "Sales Amount", "net_sales", KIND_CURRENCY,
              description="Net sales in the period."),
    MapMetric("quantity", "Sales Quantity", "quantity", KIND_QUANTITY,
              description="Units sold in the period."),
    MapMetric("volume", "Sales Volume", "volume", KIND_VOLUME,
              description="Total Volume as the sales file stated it. Blank "
                          "where no line stated one."),
    MapMetric("target_amount", "Target Amount", "target_amount", KIND_CURRENCY,
              description="Target amount set for the period."),
    MapMetric("target_quantity", "Target Quantity", "target_quantity",
              KIND_QUANTITY,
              description="Target quantity, where the target stated one."),
    MapMetric("target_volume", "Target Volume", "target_volume", KIND_VOLUME,
              description="Target volume, where the target stated one."),
    MapMetric("achievement", "Achievement %", "achievement_percent", KIND_PERCENT,
              description="Net sales as a share of target amount. Blank where "
                          "no target is set."),
    MapMetric("growth", "Growth %", "growth_percent", KIND_PERCENT, signed=True,
              description="Net sales against the comparison period. Blank "
                          "where the comparison period had no sales."),
    MapMetric("shortfall", "Shortfall", "shortfall", KIND_CURRENCY, signed=True,
              description="Net sales minus target amount: negative when short. "
                          "Blank where no target is set."),
    MapMetric("customer_count", "Customer Count", "customer_count", KIND_COUNT,
              unavailable_at=("customer",),
              description="Distinct customers with a sale in the period."),
)

METRIC_BY_KEY: dict[str, MapMetric] = {metric.key: metric for metric in METRICS}
METRIC_KEYS: tuple[str, ...] = tuple(METRIC_BY_KEY)

#: The metric the seeded design and a new layer default to.
DEFAULT_METRIC = "net_sales"
DEFAULT_COLOR_METRIC = "achievement"
DEFAULT_SIZE_METRIC = "net_sales"


def get_metric(key: str) -> MapMetric:
    """Look up a metric, raising a clear error for an unknown key."""
    metric = METRIC_BY_KEY.get(key)
    if metric is None:
        raise ValueError(
            f"Unknown map metric {key!r}. Supported: {', '.join(METRIC_KEYS)}."
        )
    return metric


def metrics_for(level: str) -> tuple[MapMetric, ...]:
    """The metrics a layer at this level may name."""
    return tuple(metric for metric in METRICS if metric.available_at(level))


def catalogue() -> list[dict[str, Any]]:
    """Every metric as the layer editor's dropdowns consume it."""
    return [metric.to_dict() for metric in METRICS]


__all__ = [
    "DEFAULT_COLOR_METRIC",
    "DEFAULT_METRIC",
    "DEFAULT_SIZE_METRIC",
    "KIND_COUNT",
    "KIND_CURRENCY",
    "KIND_PERCENT",
    "KIND_QUANTITY",
    "KIND_VOLUME",
    "METRICS",
    "METRIC_BY_KEY",
    "METRIC_KEYS",
    "MapMetric",
    "catalogue",
    "get_metric",
    "metrics_for",
]
