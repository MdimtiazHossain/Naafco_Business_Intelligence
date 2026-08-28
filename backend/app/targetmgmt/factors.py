"""The eight allocation factors, and the configuration that drives them.

**Weights are data, never literals scattered through the engine.** Every factor
is declared once in :data:`FACTORS` with its default weight and its data
requirement; :class:`FactorSettings` carries what a particular run was
configured with; and the engine asks this module for a node's weight rather than
computing one itself. Changing what drives an allocation is therefore an edit to
a configuration object, not a hunt through the allocation code.

**The eight are not eight summands, and saying so would be a lie.** Three of
them describe *how much* of a parent's volume a child should get, and combine as
a weighted mixture. The other five do different jobs:

``SHARE`` factors — Historical Sales Contribution, Previous-Year Growth Trend,
    Two-Year Average, Customer Potential, Territory Potential. Each turns the
    children of one node into a distribution that sums to 1, and the enabled
    ones are mixed by weight. Mixing distributions keeps the result a
    distribution, so the mixture cannot come to more or less than the parent.

``MODE`` factors — Monthly Seasonality, New Customer / New SKU, Management
    Adjustment. These are not terms in that sum. Seasonality acts on a different
    axis entirely (which month, not which child); the new-node factor decides
    what a child with no history is worth at all; and a management adjustment is
    applied after the mixture and re-normalised.

**Two of the eight have no data source in this platform, and are declared
anyway.** Neither ``dim_customer`` nor ``dim_territory`` carries a potential —
no land holding, no crop pattern, no rating. So Customer Potential and Territory
Potential are declared, are **off by default**, and report *Not Available — no
potential master data configured*. That is deliberate: deleting them would hide
a gap the business has asked about, and **deriving a potential from sales
history would count one signal twice**, under two names, since Historical Sales
Contribution already reads exactly that data.

They are not merely stubs. :data:`POTENTIAL_MASTER_COLUMNS` records the shape a
Potential Master would have to carry, :class:`PotentialSource` is the interface
it would implement, and :func:`set_potential_source` switches both factors on.
No allocation code changes when that day comes — the engine already asks this
module for a node's weight, and the mixture already normalises whatever a factor
returns.

**Management Adjustment is a switch here, not a mechanism.** The adjustments
themselves are node-level, absolute and stored in ``target_adjustment``; see
:mod:`app.targetmgmt.adjustments` for why a global percentage cannot mean
anything. This factor decides only whether a run reads them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from datetime import date  # noqa: F401  (referenced by PotentialSource)
from typing import Any, Mapping, Protocol

#: How a factor participates in the allocation.
SHARE = "SHARE"
MODE = "MODE"

#: What a factor needs in order to say anything.
NEEDS_SALES_HISTORY = "sales_history"
NEEDS_MONTHLY_HISTORY = "monthly_sales_history"
NEEDS_CUSTOMER_POTENTIAL = "customer_potential"
NEEDS_TERRITORY_POTENTIAL = "territory_potential"
#: Needs nothing: it is a manual instruction, not a derivation.
NEEDS_NOTHING = "none"


@dataclass(frozen=True)
class Factor:
    """One allocation factor, declared once."""

    key: str
    label: str
    kind: str
    description: str
    default_weight: float
    default_enabled: bool
    requires: str
    #: Set where the platform holds no source for this factor at all. Distinct
    #: from "the data is empty": an empty ``fact_sales`` fills up when somebody
    #: uploads sales, whereas a customer potential has nowhere to come *from*
    #: until a master states one.
    unsupported_reason: str | None = None

    @property
    def supported(self) -> bool:
        """Whether this factor has a source at all.

        The two potential factors answer this dynamically: they are unsupported
        while no Potential Master is registered and supported the moment one is,
        with no edit to their declaration. That is what makes the architecture
        ready for a master that does not exist yet.
        """
        if self.requires in (NEEDS_CUSTOMER_POTENTIAL,
                             NEEDS_TERRITORY_POTENTIAL):
            return potential_available()
        return self.unsupported_reason is None


#: The eight, in the order a planner reads them.
#:
#: Weights are the *defaults*, and are meaningful only relative to one another
#: within :data:`SHARE`. They are normalised at use, so 40/15/15 and 8/3/3 give
#: the same allocation.
FACTORS: tuple[Factor, ...] = (
    Factor(
        key="historical_contribution",
        label="Historical Sales Contribution",
        kind=SHARE,
        description=(
            "Each node's share of the most recent basis year's volume. The "
            "default driver: what a territory sold last year is the best single "
            "predictor of what it can sell next year."
        ),
        default_weight=40.0,
        default_enabled=True,
        requires=NEEDS_SALES_HISTORY,
    ),
    Factor(
        key="growth_trend",
        label="Previous-Year Growth Trend",
        kind=SHARE,
        description=(
            "Last year's volume projected forward by the year-on-year movement, "
            "so a node that grew 30% is weighted above one that stood still at "
            "the same size."
        ),
        default_weight=15.0,
        default_enabled=True,
        requires=NEEDS_SALES_HISTORY,
    ),
    Factor(
        key="two_year_average",
        label="Two-Year Average",
        kind=SHARE,
        description=(
            "The mean of both basis years, which damps a node whose latest year "
            "was an outlier in either direction."
        ),
        default_weight=15.0,
        default_enabled=True,
        requires=NEEDS_SALES_HISTORY,
    ),
    Factor(
        key="customer_potential",
        label="Customer Potential",
        kind=SHARE,
        description=(
            "A customer's capacity to sell beyond what they have sold — land "
            "holding, crop pattern, shelf space."
        ),
        default_weight=8.0,
        default_enabled=False,
        requires=NEEDS_CUSTOMER_POTENTIAL,
        unsupported_reason=(
            "Customer Potential: Not Available — no potential master data "
            "configured. Deriving one from sales history would make this factor "
            "a second copy of Historical Sales Contribution, counting the same "
            "signal twice, so it stays off until a Potential Master supplies "
            "it."
        ),
    ),
    Factor(
        key="territory_potential",
        label="Territory Potential",
        kind=SHARE,
        description=(
            "A territory's capacity beyond its history — market size, "
            "competitor presence, coverage."
        ),
        default_weight=7.0,
        default_enabled=False,
        requires=NEEDS_TERRITORY_POTENTIAL,
        unsupported_reason=(
            "Territory Potential: Not Available — no potential master data "
            "configured. Same reasoning as Customer Potential: it stays "
            "declared and off rather than being invented from the sales the "
            "other factors already read."
        ),
    ),
    Factor(
        key="monthly_seasonality",
        label="Monthly Seasonality",
        kind=MODE,
        description=(
            "Splits a year's volume across its months by the historical monthly "
            "pattern rather than by twelfths. Turning it off falls back to an "
            "equal split, which is marked as a fallback rather than presented "
            "as a seasonal profile."
        ),
        default_weight=15.0,
        default_enabled=True,
        requires=NEEDS_MONTHLY_HISTORY,
    ),
    Factor(
        key="new_node_seed",
        label="New Customer / New SKU Handling",
        kind=MODE,
        description=(
            "What a node with no history is worth. Seeded from the average of "
            "its siblings that do have history, scaled by the seed share. "
            "Turning it off gives a new customer or a new material nothing at "
            "all, which is a real choice and not the same as a bug."
        ),
        default_weight=5.0,
        default_enabled=True,
        requires=NEEDS_NOTHING,
    ),
    Factor(
        key="management_adjustment",
        label="Management Adjustment",
        kind=MODE,
        description=(
            "A per-node uplift applied after the mixture and re-normalised, so "
            "the parent's total never changes. A uniform adjustment is a no-op "
            "by construction — raising every child and re-normalising returns "
            "the distribution you started with — so this is stated per node."
        ),
        default_weight=0.0,
        default_enabled=False,
        requires=NEEDS_NOTHING,
    ),
)

FACTOR_BY_KEY: dict[str, Factor] = {factor.key: factor for factor in FACTORS}

SHARE_FACTORS: tuple[Factor, ...] = tuple(f for f in FACTORS if f.kind == SHARE)
MODE_FACTORS: tuple[Factor, ...] = tuple(f for f in FACTORS if f.kind == MODE)

# ---------------------------------------------------------------------------
# The extension point for a Potential Master that does not exist yet
# ---------------------------------------------------------------------------

#: The columns a Potential Master would have to carry for the two potential
#: factors to switch on.
#:
#: Written down — and pinned by a test — because a shape described only in prose
#: drifts. When such a master arrives, the work is: load it, implement a
#: :class:`PotentialSource` over it, register it with :func:`set_potential_source`
#: and clear ``unsupported_reason`` on the two factors. **No allocation code
#: changes**: the engine already asks this module for a node's weight, and the
#: mixture already normalises whatever each factor returns.
#:
#: ``effective_from`` / ``effective_to`` are in the list because a potential is a
#: judgement made at a point in time, and a target built for FY 2026-27 should
#: read the potential that was current then rather than whatever was last typed.
POTENTIAL_MASTER_COLUMNS: tuple[str, ...] = (
    "customer_code",
    "potential_volume",
    "potential_category",
    "effective_from",
    "effective_to",
    "source",
    "status",
)


class PotentialSource(Protocol):
    """What a Potential Master must be able to answer.

    Deliberately narrow: one question, asked per level, returning a plain number
    per node. Anything richer would let a potential source reach into the
    allocation and do more than weight a split.

    A source returning ``0.0`` for a node is saying "no potential recorded",
    which the mixture treats as no contribution — not as a claim that the node
    has none. That distinction is the caller's to make, and is why an
    implementation should omit a node it has no row for rather than return zero
    for it.
    """

    def potential_for(self, level: str, node_codes: list[str],
                      as_of: "date | None" = None) -> dict[str, float]:
        """``{node_code: potential volume}`` for the nodes it knows about."""
        ...


#: The registered source, or ``None`` while no Potential Master exists.
#:
#: Module-level and deliberately not a parameter threaded through the engine:
#: whether a deployment has a Potential Master is a property of the deployment,
#: not of one allocation run.
_potential_source: "PotentialSource | None" = None


def set_potential_source(source: "PotentialSource | None") -> None:
    """Register a Potential Master, enabling the two potential factors.

    Until one is registered the factors report ``NOT_AVAILABLE`` and cannot be
    switched on, which is what keeps a demo from quietly inventing a potential.
    """
    global _potential_source
    _potential_source = source


def potential_available() -> bool:
    return _potential_source is not None


#: What fraction of its history-bearing siblings' average a node with no history
#: is seeded at, when the new-node factor is on.
#:
#: Half, and deliberately below one: a customer nobody has sold to is not
#: assumed to perform like an established one, but neither is it assumed to be
#: worth nothing. It is a declared planning assumption rather than a fact, which
#: is why it is one named constant a reader can find and argue with.
NEW_NODE_SEED_SHARE = 0.5


@dataclass(frozen=True)
class FactorAvailability:
    """Whether one factor can actually be calculated on this run's data."""

    key: str
    label: str
    enabled: bool
    weight: float
    available: bool
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "enabled": self.enabled,
            "weight": self.weight,
            "available": self.available,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class FactorSettings:
    """What one allocation run was configured with.

    Frozen, and carried through the whole run: an allocation has to be
    reproducible, and settings that could be mutated halfway would make the
    first half of a run answer to different rules from the second.
    """

    weights: Mapping[str, float] = field(default_factory=dict)
    enabled: Mapping[str, bool] = field(default_factory=dict)

    @classmethod
    def defaults(cls) -> "FactorSettings":
        return cls(
            weights={f.key: f.default_weight for f in FACTORS},
            enabled={f.key: f.default_enabled for f in FACTORS},
        )

    @classmethod
    def from_request(cls, payload: Mapping[str, Any] | None) -> "FactorSettings":
        """Build settings from an API payload, defaulting anything unstated.

        An unsupported factor cannot be switched on from outside, whatever the
        payload says. That is not a validation nicety: enabling Customer
        Potential would give every node a weight of zero from that factor, which
        silently reduces the mixture rather than failing, and the allocation
        would quietly stop being what the settings claim it is.
        """
        payload = payload or {}
        weights = dict((f.key, f.default_weight) for f in FACTORS)
        enabled = dict((f.key, f.default_enabled) for f in FACTORS)

        for key, value in (payload.get("weights") or {}).items():
            if key in FACTOR_BY_KEY and value is not None:
                weights[key] = max(0.0, float(value))
        for key, value in (payload.get("enabled") or {}).items():
            if key in FACTOR_BY_KEY:
                enabled[key] = bool(value) and FACTOR_BY_KEY[key].supported

        return cls(weights=weights, enabled=enabled)

    def is_on(self, key: str) -> bool:
        factor = FACTOR_BY_KEY.get(key)
        if factor is None or not factor.supported:
            return False
        return bool(self.enabled.get(key, factor.default_enabled))

    def weight_of(self, key: str) -> float:
        factor = FACTOR_BY_KEY.get(key)
        if factor is None:
            return 0.0
        return float(self.weights.get(key, factor.default_weight))

    def active_share_weights(self) -> dict[str, float]:
        """The enabled SHARE factors and their weights, dropping zero weights.

        A factor that is on but weighted zero contributes nothing, so it is
        removed here rather than left to multiply into the mixture as a term
        that cannot change the answer — which would make the *number* of active
        factors, reported on the screen, disagree with the number that actually
        did anything.
        """
        active = {}
        for factor in SHARE_FACTORS:
            if not self.is_on(factor.key):
                continue
            weight = self.weight_of(factor.key)
            if weight > 0:
                active[factor.key] = weight
        return active

    def to_dict(self) -> dict[str, Any]:
        return {"weights": dict(self.weights), "enabled": dict(self.enabled)}


def availability(settings: FactorSettings, *, has_sales_history: bool,
                 has_monthly_history: bool) -> list[FactorAvailability]:
    """Which factors can be calculated on this run's data, and why not.

    Called before an allocation is generated and surfaced on the screen, so a
    planner sees "these three are driving the split and these two cannot be
    calculated" rather than discovering it from a result that looks arbitrary.
    """
    rows: list[FactorAvailability] = []
    for factor in FACTORS:
        reason: str | None = None
        available = True
        if not factor.supported:
            available = False
            reason = factor.unsupported_reason
        elif factor.requires == NEEDS_SALES_HISTORY and not has_sales_history:
            available = False
            reason = (
                "No sales were recorded in the basis years for this plan's "
                "scope, so there is no history to weight by."
            )
        elif factor.requires == NEEDS_MONTHLY_HISTORY and not has_monthly_history:
            available = False
            reason = (
                "No monthly sales pattern exists for the basis years, so a "
                "seasonal split would be an equal split wearing another name."
            )
        rows.append(FactorAvailability(
            key=factor.key, label=factor.label,
            enabled=settings.is_on(factor.key),
            weight=settings.weight_of(factor.key),
            available=available, reason=reason,
        ))
    return rows


def node_weights(history: Mapping[str, tuple[float | None, float | None]],
                 settings: FactorSettings) -> tuple[dict[str, Decimal],
                                                    dict[str, dict[str, float]]]:
    """Turn one parent's children into weights, and show each factor's part.

    ``history`` maps a child's code to ``(earlier year volume, later year
    volume)``, either of which may be ``None`` for "did not sell".

    Returns ``(weights, contributions)``. ``contributions`` is what the screen
    renders as "which factor produced this share": for each child, each active
    factor's normalised contribution before mixing. It is derived here rather
    than recomputed by the caller precisely so the explanation and the number
    cannot drift apart.

    Every SHARE factor is normalised to sum to 1 across the children *before*
    mixing, so a factor measured in volume and a factor measured in projected
    volume carry the weight the settings gave them rather than the weight their
    units happen to imply.
    """
    codes = sorted(history)
    active = settings.active_share_weights()

    raw: dict[str, dict[str, float]] = {}
    if active:
        for key in active:
            raw[key] = {code: _factor_value(key, history[code]) for code in codes}

    seeded = _seed_new_nodes(codes, raw, history, settings)

    weights: dict[str, Decimal] = {code: Decimal(0) for code in codes}
    contributions: dict[str, dict[str, float]] = {code: {} for code in codes}

    weight_total = sum(active.values()) or 1.0
    for key, factor_weight in active.items():
        values = seeded[key]
        total = sum(values.values())
        if total <= 0:
            # This factor says nothing about these children — every one of them
            # is zero. Skipped rather than divided by, and the other factors
            # carry the split.
            continue
        share_of_mixture = factor_weight / weight_total
        for code in codes:
            part = (values[code] / total) * share_of_mixture
            weights[code] += Decimal(str(part))
            if part:
                contributions[code][key] = part

    if not any(weights.values()):
        # No active factor produced anything. Left at zero deliberately: the
        # distributor falls back to an equal split and *flags* it, which is a
        # visible fallback rather than a silent one manufactured here.
        return {code: Decimal(0) for code in codes}, contributions

    # No management adjustment is applied here. An adjustment is absolute and
    # node-level, so it acts on the distributed *volumes* rather than on the
    # weights that produced them — see ``adjustments.apply_to_siblings``.
    # Folding it in as a weight multiplier would make it a percentage again,
    # and a percentage re-normalised is the no-op this design exists to avoid.
    return weights, contributions


def _factor_value(key: str,
                  years: tuple[float | None, float | None]) -> float:
    """One child's raw value under one SHARE factor, before normalisation."""
    earlier, later = years
    earlier_value = earlier or 0.0
    later_value = later or 0.0

    if key == "historical_contribution":
        return later_value
    if key == "two_year_average":
        measured = [value for value in (earlier, later) if value is not None]
        return sum(measured) / len(measured) if measured else 0.0
    if key == "growth_trend":
        # Last year projected forward by its own movement. Floored at zero: a
        # node that halved does not get a negative share of a positive target.
        if earlier_value <= 0:
            return later_value
        growth = (later_value - earlier_value) / earlier_value
        return max(0.0, later_value * (1 + growth))
    # customer_potential / territory_potential. While no Potential Master is
    # registered these are never active — ``is_on`` refuses to enable an
    # unsupported factor — so this line is reached only once a source exists,
    # and it returns zero for a node that source has no row for. **Sales history
    # is never consulted here**: using it as "potential" would count one signal
    # twice, under two names, which is the whole reason the factor is not
    # simply derived.
    return 0.0


def _seed_new_nodes(codes: list[str], raw: dict[str, dict[str, float]],
                    history: Mapping[str, tuple[float | None, float | None]],
                    settings: FactorSettings) -> dict[str, dict[str, float]]:
    """Give a child with no history something, or nothing, as configured.

    A new customer scores zero under every history-based factor, so without
    this it receives no target at all — which is right if the business does not
    want to plan for it and wrong if it has just been signed. The factor makes
    that a decision rather than an accident.
    """
    if not settings.is_on("new_node_seed") or not raw:
        return raw

    seeded = {key: dict(values) for key, values in raw.items()}
    share = NEW_NODE_SEED_SHARE * (settings.weight_of("new_node_seed") / 5.0
                                   if settings.weight_of("new_node_seed") else 1.0)
    share = max(0.0, min(share, 1.0))

    for key, values in seeded.items():
        established = [values[code] for code in codes
                       if any(v for v in history[code] if v)]
        if not established:
            continue
        average = sum(established) / len(established)
        for code in codes:
            if not any(v for v in history[code] if v):
                values[code] = average * share
    return seeded


def describe() -> list[dict[str, Any]]:
    """The catalogue, for the settings screen. Derived, never restated there."""
    return [
        {
            "key": factor.key,
            "label": factor.label,
            "kind": factor.kind,
            "description": factor.description,
            "default_weight": factor.default_weight,
            "default_enabled": factor.default_enabled,
            "requires": factor.requires,
            "supported": factor.supported,
            "unsupported_reason": factor.unsupported_reason,
        }
        for factor in FACTORS
    ]


__all__ = [
    "SHARE",
    "MODE",
    "Factor",
    "FACTORS",
    "FACTOR_BY_KEY",
    "SHARE_FACTORS",
    "MODE_FACTORS",
    "NEW_NODE_SEED_SHARE",
    "POTENTIAL_MASTER_COLUMNS",
    "PotentialSource",
    "set_potential_source",
    "potential_available",
    "FactorAvailability",
    "FactorSettings",
    "availability",
    "node_weights",
    "describe",
]
