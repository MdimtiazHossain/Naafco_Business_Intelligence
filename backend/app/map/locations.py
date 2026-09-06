"""Every placed coordinate, by level, with no figure attached to any of it.

This is what the Area Demarcation tab draws. It is a different question from
:mod:`app.map.data`'s, and the difference is the whole point: that module
answers "what did this level *do* in this period, and where is it", and this
one answers only "where is it". No fact table is read, no period applies, and
no metric is computed — so nothing here can disagree with a report, because
nothing here states a figure.

**Why this is not a tool.** ``get_map_layer`` exists in ``ai/tools.py`` because
the analysis map aggregates sales and targets, and every path to a figure in
this platform goes through the tool layer so a map and a report cannot come to
different numbers. This read touches ``map_entity_locations`` and the master
dimensions and nothing else. Putting it behind the tool registry would suggest
it is a query surface that could drift from a report; it cannot, because it
computes nothing to drift.

**Scope is the outer bound and a filter narrows inside it.**

An earlier version of this module was unscoped, on the argument that you judge
a boundary by seeing both sides of it and a map clipped to your own region
cannot do that. That argument was overruled once the tab became narrowable by
organisational level, and the reason is worth keeping: a national customer map
is a different artefact from the individual lookups that could already reach
each of its rows, and "you can already fetch these one at a time" is not a
reason to hand over all of them at once.

So the caller's data scope is the ceiling, the reader's filter narrows within
it, and a filter naming something outside it is a **refusal that names the
code** — never an empty map, which reads as "there is nothing there" when the
truth is "you may not see that". A reader with no scope at all gets the
explanation :func:`app.targetmgmt.review._describe_scope` already words, not an
error and not somebody else's coordinates.

**A filter narrows by containment, not by row.** This is the one place this
module deliberately parts company with every report table in the platform, and
it needs saying because the flat rule looks correct until you try it. A report
ANDs its filters and a row matches only on a level it actually carries, so
filtering by Region drops every row stating no region — which here would delete
the zone above and every customer below, because a coordinate row names one
level and nothing else. Selecting a region on a *map* means "this region and
what is inside it". So the filter resolves to a **subtree**: the selected
node's own coordinate and every level beneath it, never its ancestors.
:func:`subtree_codes` is that shape and says how it differs from
:func:`app.org.hierarchy.resolve_org_scope`, which supplies both directions.

**A level with no coordinates says so.** An empty layer carries a note naming
what to load, because a blank map and a map of nothing look identical and only
one of them is a data problem. The deployment has no customer coordinates at
all and ``data/dev.db`` has 846, so this is the ordinary case, not an edge one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.exceptions import PermissionDeniedError
from ..ai.permission_filter import (
    FILTER_FIELD_BY_LEVEL,
    PermissionFilter,
    UserContext,
)
from ..ai.schemas import ScopeFilters
from ..database.models_map import GeoSource, MapEntityLocation
from ..org.hierarchy import (
    ORG_CHAIN,
    resolve_business_entities,
    resolve_org_scope,
)
from . import geo
from .levels import MAP_LEVELS, MapLevel, get_level


@dataclass
class LocationLayer:
    """One level's placed coordinates, and what is missing from them."""

    level: str
    label: str
    #: GeoJSON point features, one per placed entity. Longitude first.
    features: list[dict[str, Any]] = field(default_factory=list)
    #: How many entities the master holds at this level, placed or not —
    #: inside the caller's scope, like every other count on this layer.
    #:
    #: Unscoped it reported the national row count, so a region-scoped reader
    #: saw "8 of 9 region records have no coordinate" about eight regions that
    #: are placed and simply not theirs.
    total: int = 0
    #: How many coordinates exist at this level inside the caller's scope,
    #: before the reader's own filter narrowed them.
    #:
    #: The second query the matched/total pair needs. Without it "9 points"
    #: cannot be told apart from "9 points and there are 94" — which is the
    #: difference between a narrow filter and a barely-mapped level, and the
    #: reader has no other way to know which they are looking at.
    available: int = 0
    #: How many of the placed coordinates are centroids rather than positions
    #: somebody stated. Reported separately because the renderer draws them
    #: differently and a reader has to be able to count them.
    derived: int = 0
    bounds: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def placed(self) -> int:
        return len(self.features)

    @property
    def missing(self) -> int:
        """Records with no coordinate — never records a filter excluded.

        Against ``available`` rather than ``placed``: a placed coordinate the
        reader filtered out has not gone missing, and counting it here would
        report a data problem every time somebody narrowed the map.
        """
        return max(0, self.total - self.available)

    def feature_collection(self) -> dict[str, Any]:
        return {"type": "FeatureCollection", "features": self.features}

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "label": self.label,
            "features": self.feature_collection(),
            "total": self.total,
            "placed": self.placed,
            "available": self.available,
            "derived": self.derived,
            "missing": self.missing,
            "bounds": self.bounds,
            "notes": self.notes,
        }


@dataclass
class DemarcationData:
    """Several levels of coordinates, in the order asked for."""

    layers: list[LocationLayer] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not any(layer.features for layer in self.layers)

    def to_dict(self) -> dict[str, Any]:
        return {
            "empty": self.empty,
            "layers": [layer.to_dict() for layer in self.layers],
        }


#: The filter levels a coordinate can be narrowed by, in hierarchy order.
#:
#: Derived from :data:`app.map.levels.MAP_LEVELS` rather than written out, so a
#: level added to the chain becomes filterable here with no second list to keep
#: in step — the failure the top of ``CLAUDE.md`` opens with. The browser is
#: sent this same list through ``GET /api/map/config`` for the same reason.
FILTER_LEVELS: tuple[str, ...] = tuple(
    f"{level.key}_code" for level in MAP_LEVELS
)

#: Filter level -> the field ``ScopeFilters`` spells it with.
#:
#: The two differ by more than an ``s``: ``bu_code`` is ``business_unit_codes``.
#: Read from :data:`app.ai.permission_filter.FILTER_FIELD_BY_LEVEL` rather than
#: derived by pluralising, which is what an earlier version of this module did —
#: it matched nothing, so every filter silently drew the whole map while the
#: chip above it claimed a narrowing.
_FIELD_BY_FILTER: dict[str, str] = {
    **FILTER_FIELD_BY_LEVEL,
    "customer_code": "customer_codes",
    "sales_force_code": "sales_force_codes",
}


@dataclass(frozen=True)
class Subtree:
    """Which codes at which levels a selection contains."""

    #: ``{level key: {code}}`` — absent means "no narrowing at this level".
    codes: dict[str, set[str]]
    #: The deepest level the reader actually filtered on, or ``None``.
    selected_level: str | None
    #: True when the filter names a real combination that contains nothing.
    empty: bool
    #: How the caller's own data scope narrowed things, in words.
    scope_note: str | None = None
    #: The same shape, resolved from the caller's data scope **alone**.
    #:
    #: The denominator of "9 of 94", and it has to be a second containment
    #: rather than the level's own row count. ``available`` counted every
    #: coordinate at the level, so a regional manager filtering to one of their
    #: own territories read "16 of 94" where 94 was the national figure: a
    #: total they may not see, printed under a label saying it was theirs.
    #: Scope is the outer bound, so it bounds what a reader is told exists just
    #: as it bounds what they are shown.
    #:
    #: Empty for an unrestricted caller, which :meth:`allows_in_scope` reads as
    #: "no narrowing" — the same convention ``codes`` uses.
    scope_codes: dict[str, set[str]] = field(default_factory=dict)
    #: True when the reader filtered, as against their role having scoped them.
    #:
    #: Both narrow the map and they read the same in ``codes``, but they are
    #: not the same finding: a filter is something the reader did and can undo,
    #: a scope is not. Telling somebody to "clear it to see this level" when
    #: what emptied the level was their own permissions is advice they cannot
    #: take.
    filtered: bool = False

    def allows(self, level_key: str, code: str) -> bool:
        wanted = self.codes.get(level_key)
        return wanted is None or code in wanted

    def allows_in_scope(self, level_key: str, code: str) -> bool:
        """Inside the caller's scope, before their own filter narrowed it."""
        wanted = self.scope_codes.get(level_key)
        return wanted is None or code in wanted


def _describe_scope(user: UserContext) -> str | None:
    """The three-case sentence, matching Target Management's wording."""
    if user.is_unrestricted:
        return None
    if not user.data_scope:
        return ("Your account has no data scope, so no coordinates are visible "
                "to you. An administrator grants one.")
    return f"Scoped to {user.describe_scope()} by your role"


def subtree_codes(session: Session, user: UserContext,
                  filters: ScopeFilters | None) -> Subtree:
    """The selection's own node and everything beneath it, bounded by scope.

    **Not** :func:`app.org.hierarchy.resolve_org_scope`'s shape, though it is
    built from it. That function answers "which codes are involved", which
    includes the *ancestors* of the selection — a region's zone, sales line,
    business unit and company — because the analysis map needs them to place a
    point in a hierarchy. A demarcation map asking "show me R01" means R01 and
    what is inside it; drawing its zone too would put a point on the map that
    the reader did not ask for and cannot act on, and drawing every other region
    of that zone (which the zone's own subtree contains) would be worse.

    So the resolver runs, and then everything **above** the deepest selected
    level is dropped. Below it, the codes are already only descendants, because
    the path rows were filtered by the selection first.

    The caller's data scope is merged in as a filter of its own before any of
    this, so it bounds the result rather than being applied to it afterwards:
    a reader may narrow inside their scope and cannot widen out of it.

    **Two containments come back, not one.** ``codes`` is what to draw;
    ``scope_codes`` is the same resolution over the scope alone, and it is what
    the layer counts report against. Without it a scoped reader filtering to
    one of their own territories was told "16 of 94" — a national denominator
    they are not entitled to, under a label claiming it was theirs.
    """
    supplied: dict[str, Any] = {}
    # `field_name`, not `field`: this module imports `dataclasses.field`, and a
    # loop variable of that name shadows it. Harmless today because `Subtree`
    # is built at import time, and exactly the kind of harmless that stops
    # being so when somebody adds a default to a dataclass declared below.
    for filter_level, field_name in _FIELD_BY_FILTER.items():
        codes = (getattr(filters, field_name, None)
                 if filters is not None else None)
        if codes:
            supplied[filter_level.removesuffix("_code")] = list(codes)
    scope_note = _describe_scope(user)
    # The caller's scope as a selection of its own. Empty for an unrestricted
    # reader, for whom "what exists" and "what exists for you" are one question.
    scope_supplied: dict[str, Any] = {}
    # Whether the *reader* narrowed anything, recorded before the scope is
    # merged in below — afterwards the two are indistinguishable.
    filtered = any(level in ORG_CHAIN for level in supplied)

    # No scope at all: the explained empty view, not an error and not a map.
    # Nothing is visible and nothing exists to be counted, so both containments
    # are empty — "0 of 0", never "0 of 94".
    if not user.is_unrestricted and not user.data_scope:
        nothing = {level: set() for level in ORG_CHAIN}
        return Subtree(codes=nothing, selected_level=None, empty=True,
                       scope_note=scope_note, scope_codes=dict(nothing),
                       filtered=filtered)

    # Scope first, so a filter outside it is refused rather than silently
    # widening the result.
    #
    # The containment test is `PermissionFilter`'s own, not a comparison
    # written here: a scoped region must admit its territories, and a scoped
    # territory must admit the region *above* it as a narrowed slice. An
    # equality check would refuse both, and a second copy of that walk would
    # drift from the one every report and filter dropdown already shares.
    if not user.is_unrestricted:
        permissions = PermissionFilter(session, user)
        for level, value in list(supplied.items()):
            if level not in ORG_CHAIN:
                continue
            for code in _as_codes(value):
                # `f"{level}_code"`, because that is how the permission layer
                # spells a level: `LEVEL_DEPTH` and `BINDING_BY_LEVEL` are keyed
                # on `LevelBinding.code_field`. Passing the bare `region` found
                # nothing in either, so `_ancestors` returned an empty chain and
                # the check answered False for *every* code — a scoped reader
                # was refused their own region, with a message that then said
                # "your access covers region REG001". A test asserting a refusal
                # passed on it, which is why the one below now also asserts the
                # allowed case.
                if not permissions.is_within_scope(f"{level}_code", code):
                    raise PermissionDeniedError(
                        f"user {user.username} denied {level}={code}",
                        user_message=(
                            f"You don't have permission to see coordinates for "
                            f"{code}. Your access covers {user.describe_scope()}."
                        ),
                    )
        # The scope itself becomes a filter, so it bounds the subtree rather
        # than being applied to it afterwards. `data_scope` holds a *list* of
        # codes per level — several territories is an ordinary grant — and
        # `resolve_org_scope` unions codes at one level, which is what a
        # multi-code scope means.
        #
        # Kept in `scope_supplied` as well, because the scope alone is a
        # selection in its own right: it is the denominator the layer counts
        # report against. A level the reader also filtered on is not copied
        # into `supplied` — there the filter is narrower than the scope by
        # construction, having been checked against it just above.
        for scope_level, codes_at_level in (user.data_scope or {}).items():
            key = scope_level.removesuffix("_code")
            if key in ORG_CHAIN:
                scope_supplied[key] = list(codes_at_level)
                if key not in supplied:
                    supplied[key] = list(codes_at_level)

    codes, selected_level, empty = _contained_codes(session, supplied)

    # The same resolution over the scope alone, which is what the reader is
    # told exists. Skipped when the filter added nothing to it — an unfiltered
    # request from a scoped reader, and every request from an unrestricted one
    # — because the two passes would otherwise be the same queries twice.
    if supplied == scope_supplied:
        scope_codes = codes
    else:
        scope_codes, _, _ = _contained_codes(session, scope_supplied)

    return Subtree(codes=codes,
                   selected_level=selected_level,
                   empty=empty,
                   scope_note=scope_note,
                   scope_codes=scope_codes,
                   filtered=filtered)


def _contained_codes(session: Session, supplied: dict[str, Any],
                     ) -> tuple[dict[str, set[str]], str | None, bool]:
    """One selection's subtree: ``{level: {code}}``, its deepest level, empty.

    Split out of :func:`subtree_codes` because it is run twice — once over the
    reader's filter and once over their data scope alone — and the two have to
    agree about what containment means, or the count and the map would not.
    """
    org_filters = {level: value for level, value in supplied.items()
                   if level in ORG_CHAIN}
    scope = resolve_org_scope(session, org_filters)

    codes: dict[str, set[str]] = {}
    if org_filters:
        deepest = scope.selected_level or max(
            org_filters, key=lambda level: ORG_CHAIN.index(level))
        floor = ORG_CHAIN.index(deepest)
        # **Every** level gets an explicit set once a filter exists, even where
        # the resolver matched nothing. An absent key means "not narrowed here"
        # and would let the level through whole — so a hierarchy with no
        # company row above it (the resolver walks down from `dim_company`)
        # drew every region on the map while claiming to show one. A filter
        # that matches nothing must empty the layer, not disable itself.
        for level in ORG_CHAIN:
            codes[level] = set()
        for entity_type in ("customer", "sales_force"):
            codes[entity_type] = set()
        # Everything above the selection stays empty: a subtree, not a path.
        for level, level_codes in scope.codes.items():
            if level in ORG_CHAIN and ORG_CHAIN.index(level) >= floor:
                codes[level] = set(level_codes)
        # Customer and sales force hang off the chain rather than sitting in
        # it, so they are resolved through their own dimension's parent column.
        for entity_type, entities in resolve_business_entities(
                session, scope).items():
            codes[entity_type] = {entity.code for entity in entities}
    for level, value in supplied.items():
        if level not in ORG_CHAIN:
            codes[level] = set(_as_codes(value))

    return codes, scope.selected_level, bool(org_filters) and scope.empty


def _as_codes(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _master_rows(session: Session, level: MapLevel
                 ) -> tuple[dict[str, tuple[str | None, str | None]], int]:
    """``{code: (name, parent code)}`` for a level, and how many rows it has.

    One statement per level rather than one per entity: a customer layer is
    2,091 rows in the master and asking for each one's name individually is the
    shape of query that makes a page take a minute.
    """
    code = getattr(level.model, level.code_field)
    name = getattr(level.model, level.name_field, None)
    parent = (getattr(level.model, level.parent_code_field, None)
              if level.parent_code_field else None)

    columns = [code]
    columns.append(name if name is not None else code)
    if parent is not None:
        columns.append(parent)

    rows: dict[str, tuple[str | None, str | None]] = {}
    for row in session.execute(select(*columns)).all():
        row_code = str(row[0])
        row_name = str(row[1]) if row[1] is not None else None
        row_parent = str(row[2]) if parent is not None and row[2] else None
        rows[row_code] = (row_name, row_parent)
    return rows, len(rows)


def layer_locations(session: Session, level_key: str,
                    subtree: Subtree | None = None) -> LocationLayer:
    """Every placed coordinate at one level inside a selection, as GeoJSON.

    ``subtree`` is the containment filter; ``None`` draws the whole level,
    which is what an unfiltered map asks for. Raises :class:`ValueError` for a
    level the map does not draw.
    """
    level = get_level(level_key)
    layer = LocationLayer(level=level.key, label=level.label)

    master, _ = _master_rows(session, level)
    # Scoped, for the reason the field says. `allows_in_scope`, not `allows`:
    # this is what the reader may see, not what they asked for — the filter
    # narrows `placed` and must not make records look unloaded.
    layer.total = sum(
        1 for code in master
        if subtree is None or subtree.allows_in_scope(level.key, code)
    )
    placed = geo.locations_for(session, level.key)
    # Everything placed at this level inside the caller's scope, before their
    # own filter — the "of how many" the counts report against. Read from the
    # same dict rather than a second round trip.
    #
    # Through `allows_in_scope`, not `len(placed)`. The level's own row count
    # is a national figure, and printing it beside a scoped reader's nine
    # points tells them a total they may not see under a label saying it is
    # theirs. Scope bounds what a reader is told exists exactly as it bounds
    # what they are shown.
    layer.available = sum(
        1 for code in placed
        if subtree is None or subtree.allows_in_scope(level.key, code)
    )

    points: list[tuple[float, float]] = []
    for code, location in sorted(placed.items()):
        if subtree is not None and not subtree.allows(level.key, code):
            continue
        name, parent_code = master.get(code, (None, None))
        if location.source == GeoSource.DERIVED:
            layer.derived += 1
        layer.features.append({
            "type": "Feature",
            "id": f"{level.key}:{code}",
            # GeoJSON is longitude first, stated once here rather than in the
            # renderer that would otherwise get it the wrong way round.
            "geometry": {"type": "Point",
                         "coordinates": [location.longitude, location.latitude]},
            "properties": {
                "code": code,
                # The coordinate's own label first: it is what whoever placed
                # the point called it, and a master renamed since should not
                # silently relabel somebody's pin.
                "name": location.label or name or code,
                "level": level.key,
                "parent_level": level.parent,
                "parent_code": parent_code,
                "source": location.source,
                "precision": location.precision,
                "derived_from": location.derived_from,
            },
        })
        points.append((location.latitude, location.longitude))

    bounds = geo.bounds_of(points)
    layer.bounds = bounds.to_dict() if bounds else None

    noun = level.label.lower()
    narrowed = subtree is not None and bool(subtree.codes)

    # Asked first, and before any count: a level above the selection is empty
    # *by the containment rule*, whatever its own data says. Selecting a region
    # empties zone, sales line, business unit and company because a subtree
    # shows what is inside a selection and never what is above it — and read
    # through the counts instead it came out as "none of the 4 placed zone
    # coordinates is inside region X", which is a sentence about the data.
    above = _above_selection(level, subtree)
    if above and subtree is not None and subtree.filtered:
        layer.notes.append(
            f"{level.label} sits above {_selection_phrase(subtree)}, and a map "
            f"narrows to what is inside a selection. Clear it to see this level."
        )
    elif above:
        # Emptied by the reader's role rather than by anything they chose. The
        # scope note below says so once, for the whole map; four levels each
        # repeating it in their own words would be the same sentence four
        # times, and none of them would name an action the reader can take.
        pass
    elif layer.total == 0 and narrowed:
        layer.notes.append(
            f"No {noun} sits inside {_selection_phrase(subtree)}."
        )
    elif layer.total == 0:
        # Nothing to place, which is a master-data fact rather than a map one.
        layer.notes.append(
            f"No {noun} records exist yet, so there is nothing to place."
        )
    elif layer.available == 0 and narrowed:
        # Still a finding, but about this selection rather than about the
        # level: telling a regional manager that nobody has surveyed a level
        # they can only see part of would be wrong twice over.
        layer.notes.append(
            f"None of the {layer.total} {noun} records inside "
            f"{_selection_phrase(subtree)} has a coordinate."
        )
    elif layer.available == 0:
        # A level with no coordinates at all is a finding on a demarcation map,
        # not an empty result: it is the level somebody still has to survey.
        layer.notes.append(
            f"None of the {layer.total} {noun} records has a coordinate. Load "
            f"them through the Upload Centre's Map Locations file, or place "
            f"them one at a time in Data Management."
        )
    elif layer.placed == 0 and narrowed:
        # The filter emptied it, not the data. Saying which selection did it is
        # the difference between "there is nothing there" and "look elsewhere".
        layer.notes.append(
            f"None of the {layer.available} placed {noun} coordinates is "
            f"inside {_selection_phrase(subtree)}."
        )
    elif narrowed and layer.placed < layer.available:
        layer.notes.append(
            f"{layer.placed} of {layer.available} placed {noun} coordinates "
            f"are inside {_selection_phrase(subtree)}."
        )
    elif layer.missing:
        # The noun pluralises on the total it is counted out of and the verb on
        # the count itself: "1 of 17 territory records has", never "1 of 17
        # territory record has".
        layer.notes.append(
            f"{layer.missing} of {layer.total} {noun} "
            f"{'record' if layer.total == 1 else 'records'} "
            f"{'has' if layer.missing == 1 else 'have'} no coordinate and "
            f"cannot be drawn."
        )
    if subtree is not None and subtree.scope_note and layer.placed == 0:
        layer.notes.append(subtree.scope_note)
    return layer


def _above_selection(level: MapLevel, subtree: Subtree | None) -> bool:
    """True when the containment rule, not the data, is what emptied a level.

    A subtree is the selected node and everything under it, so every level
    above the selection is empty by construction. That is the map behaving as
    specified, and it needs a different sentence from a level nobody has
    surveyed — the two look identical on screen and send a reader to opposite
    places.
    """
    if subtree is None or subtree.selected_level is None:
        return False
    if level.key not in ORG_CHAIN or subtree.selected_level not in ORG_CHAIN:
        return False
    return ORG_CHAIN.index(level.key) < ORG_CHAIN.index(subtree.selected_level)


def _selection_phrase(subtree: Subtree | None) -> str:
    """Name the selection that narrowed a layer, for the note that says so.

    "No data" is the answer this platform refuses everywhere else: a reader who
    filtered to a region and saw nothing needs to know it was *their filter*,
    not the warehouse.
    """
    if subtree is None or not subtree.selected_level:
        return "the current selection"
    codes = sorted(subtree.codes.get(subtree.selected_level, ()))
    label = get_level(subtree.selected_level).label.lower()
    if not codes:
        return f"the selected {label}"
    if len(codes) == 1:
        return f"{label} {codes[0]}"
    return f"the {len(codes)} selected {label} codes"


def demarcation_data(session: Session, level_keys: Sequence[str],
                     subtree: Subtree | None = None) -> DemarcationData:
    """Several levels at once, in the order asked for.

    **One call for every level**, which is the opposite of what the analysis
    map does — and deliberately, because the cost profile is the opposite.
    There, each layer is three aggregates over ``fact_sales`` and
    ``fact_target``, so one request per layer lets the first paint while the
    rest are still running. Here every layer is an index scan over a table with
    a four-figure row count, and splitting it would buy a round trip per level
    to save nothing.
    """
    data = DemarcationData()
    for level_key in level_keys:
        data.layers.append(layer_locations(session, level_key, subtree))
    return data


def drawable_levels(session: Session) -> list[str]:
    """Every level that has at least one coordinate, in hierarchy order.

    Derived from :data:`app.map.levels.MAP_LEVELS` and the coordinates that
    exist, never from a written-down list: a level added to the organisational
    chain becomes drawable here with no second place to keep in step.
    """
    placed = {
        row[0] for row in session.execute(
            select(MapEntityLocation.entity_type).distinct()
        ).all()
    }
    return [level.key for level in MAP_LEVELS if level.key in placed]


__all__ = [
    "DemarcationData",
    "LocationLayer",
    "demarcation_data",
    "drawable_levels",
    "layer_locations",
]
