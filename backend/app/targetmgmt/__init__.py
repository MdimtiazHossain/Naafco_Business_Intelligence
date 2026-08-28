"""Target Management: building a target, rather than reading one.

The rest of the platform *reports* targets — ``fact_target`` holds what a file
stated and ``/api/pages/target`` shows achievement against it. This package is
where a target is created: a country volume typed per material, allocated down
the hierarchy, reviewed, revised, approved, versioned and finally locked back
into ``fact_target``.

The tables are declared in :mod:`app.database.models_target` and created by
revision ``0027_target_management``. Nothing here writes to ``fact_target``
until a version is locked, so today's Target page and the AI assistant keep
seeing exactly one authoritative number throughout.

Modules:

``errors``
    The refusals this package raises, each with a message safe to show a user.
``plans``
    Plans and their versions: the scope, the version chain and the status
    transitions between them.
``country``
    The country target: the one figure here a person types, and the quantity and
    value derived from it.
``history``
    Two financial years of actual sales per material, and the allocation basis
    they imply. Reads only; nothing here writes.
``datastate``
    The five states a piece of data can be in - no data, insufficient, invalid,
    a valid zero, not available - and why collapsing them lies.
``readiness``
    The pre-flight gate: what an allocation would find, before it runs.
``adjustments``
    Node-level management adjustments, stored as inputs to the engine.
``rounding``
    Largest-remainder distribution: children that sum to their parent exactly.
``factors``
    The eight allocation factors and the configuration that drives them.
``seasonality``
    How a year's volume is spread across its months, and the named fallbacks.
``engine``
    The allocation itself: country volume down to customer, month by month.
``review``
    The hierarchical review tree: every node of an allocation, and whether
    it agrees with the sum of its children.
``matrix``
    The approval chain as configuration: who signs at which level, in what
    order, within what adjustment limit.
``revisions``
    A request to change one node's figure, its routing by adjustment limit,
    and the exact sibling-funded change an approval applies.
``approvals``
    Walking the chain: submitting, signing, sending back, and the three
    conditions final approval waits on.
``lock``
    The act that writes an approved allocation into ``fact_target`` and
    freezes the derived quantity and value that were agreed.
``bulk``
    Loading a country target from a file — the same act as typing it, through
    the same validator.
``compare``
    What moved between two versions of one plan, node by node.
``dashboard``
    Where every plan has got to, and what is waiting on somebody.
``reconcile``
    Proving a parent equals the sum of its children, with zero tolerance.
``jobs``
    Running an allocation in the background and reporting where it got to.
``audit``
    The append-only business trail every action writes to.
"""

from . import (
    adjustments,
    approvals,
    audit,
    bulk,
    compare,
    country,
    dashboard,
    datastate,
    engine,
    errors,
    factors,
    history,
    jobs,
    lock,
    matrix,
    plans,
    readiness,
    reconcile,
    review,
    revisions,
    rounding,
    seasonality,
)

__all__ = [
    "adjustments",
    "approvals",
    "audit",
    "bulk",
    "compare",
    "country",
    "dashboard",
    "datastate",
    "engine",
    "errors",
    "factors",
    "history",
    "jobs",
    "lock",
    "matrix",
    "plans",
    "readiness",
    "reconcile",
    "review",
    "revisions",
    "rounding",
    "seasonality",
]
