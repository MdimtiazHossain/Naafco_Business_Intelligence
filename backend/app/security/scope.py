"""Data-scope dimensions — what a scope may name, and what contains what.

A data scope is one JSON object on ``app_user``::

    {"region_code": ["REG001"]}

Until now every level it could name came from a single chain, the organisational
hierarchy in :data:`app.etl.mapping.LEVEL_BINDINGS`, so "which level is deeper"
and "is this code inside that one" each had one answer and both lived there.

**Stock is where that stopped being true.** ``vw_material_stock_detail`` carries
a company, a plant and a storage location and none of the sales hierarchy — the
source states none, and revision 0016 says so — so a scope that can narrow a
stock report is a scope over *plants*: a second chain reaching the same
companies by a different route.

Adding ``plant_code`` to the organisational list would not have worked, and —
worse — it would not have failed loudly:

* ``LEVEL_DEPTH.get("plant_code", 0)`` reads as the **shallowest** level of all,
  so ``_scope_level_is_redundant`` would have judged a plant scope already
  implied by any deeper level the caller named and dropped it from the query. A
  scope silently not applied is the one failure mode this package exists to
  prevent.
* ``MasterDataIndex.ancestors_of`` walks ``BINDING_BY_LEVEL``, which has no
  plant, so it answers "no ancestors" and the containment test comes back False
  for **every** plant code — refusing a plant-scoped user everything, while a
  test that only asserts refusals passes on it. That is the ``region`` versus
  ``region_code`` defect this codebase has already paid for once.

So a scope level belongs to a **dimension**, and each dimension owns its order
and its master:

``org``
    ``company -> business unit -> sales line -> zone -> region -> area -> unit
    -> territory -> sub-territory``, derived from ``LEVEL_BINDINGS`` rather than
    restated, because a second copy of the chain the warehouse is built on could
    drift from it.

``plant``
    ``company -> plant -> storage location``, the chain
    :data:`app.api.routes_masterdata.FILTER_PARENTS` and
    :mod:`app.api.filter_space` already describe for the filter bar.

``company_code`` belongs to **both**, exactly as it does in ``filter_space``:
it is the one level every structure carries, and so the only one through which
one dimension can say anything about another.

Two rules follow, and step 2 of this work is where they are enforced:

1. **Depth and redundancy are computed within a dimension, never across it.**
   A plant is not shallower than a region; the question does not arise.
2. **Containment is asked of every dimension that both holds the level and is
   constrained by the caller's scope, and all of them must agree.** A dimension
   the scope says nothing about abstains rather than refusing — a region-scoped
   user's scope makes no claim about plants, and reading silence as denial would
   refuse them the whole stock page.

Nothing here decides *whether* a view may drop a scope it cannot express. That
is :func:`app.reporting.credit.assert_scope_is_honourable`'s question, and this
module exists to give it something better than a hand-written list to ask.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database.models import DimPlant, DimStorageLocation
from ..etl.mapping import LEVEL_BINDINGS, MasterDataIndex


class ScopePolicy(str, Enum):
    """What a report does about the part of a scope it cannot express.

    ``queries.filter_conditions`` skips a filter naming a column the view does
    not carry. For an optional narrowing that is right; for a scope it is a
    silent drop, and the caller is answered with everybody's figures. So every
    report declares which of the two honest answers it gives instead.

    Both are correct somewhere, and the difference is not a matter of how
    careful the report is:

    ``REFUSE``
        The dataset *is* held at a granularity the caller's scope cannot be
        checked against, so answering would disclose figures they have no claim
        to. A sales report against a plant-scoped reader is this: the sale
        states a territory and a customer, and none of it is theirs to see.

    ``DISCLOSE``
        The dataset genuinely has no such dimension and no finer truth is being
        withheld. Material stock is this, deliberately: it is not held below
        company, so an organisationally scoped reader sees the whole of what
        exists and a note says so. Refusing instead would take the stock page
        away from every regional manager in the business to protect nothing.
    """

    REFUSE = "REFUSE"
    DISCLOSE = "DISCLOSE"


#: Levels whose column name is not what a person calls them. ``bu_code`` is the
#: only one, and it renders as "bu" without this — which is not a word, in a
#: sentence whose whole job is telling somebody which level of their access to
#: take to an administrator.
_LEVEL_LABELS: dict[str, str] = {"bu_code": "business unit"}


def level_label(code_field: str) -> str:
    """``region_code`` -> ``region``; ``storage_location_key`` -> ``storage location``."""
    if code_field in _LEVEL_LABELS:
        return _LEVEL_LABELS[code_field]
    return code_field.removesuffix("_code").removesuffix("_key").replace("_", " ")


def describe_levels(levels: Sequence[str]) -> str:
    """A refusal's list of levels, as a person would read it."""
    return ", ".join(level_label(level) for level in levels)


@dataclass(frozen=True)
class ScopeLevel:
    """One level a data scope may name, and the tool filter it becomes."""

    #: The column the warehouse knows it by — ``region_code``, ``plant_code``.
    #: This is what a ``data_scope`` key spells, and getting it wrong by one
    #: suffix is how a scoped reader comes to be refused their own region.
    code_field: str
    #: The :class:`app.ai.schemas.ScopeFilters` attribute it sets. Named rather
    #: than derived at the call site because one of them is irregular
    #: (``bu_code`` sets ``business_unit_codes``), and a rule with its exception
    #: written down is safer than a rule applied hopefully.
    filter_field: str
    #: Whether an administrator may assign a scope at this level. A storage
    #: location is here so that a plant scope can *contain* one, not so that
    #: anybody can be scoped to a single shelf.
    grantable: bool = True


#: The one irregular organisational name. Every other level's filter field is
#: its code field with ``_code`` traded for ``_codes``.
_ORG_FILTER_FIELDS: dict[str, str] = {"bu_code": "business_unit_codes"}


def _org_filter_field(code_field: str) -> str:
    return _ORG_FILTER_FIELDS.get(
        code_field, code_field.removesuffix("_code") + "_codes")


#: The organisational levels, *derived* from the bindings the warehouse is built
#: on. Restating them here would be a second list to keep in step with the first.
ORG_LEVELS: tuple[ScopeLevel, ...] = tuple(
    ScopeLevel(binding.code_field, _org_filter_field(binding.code_field))
    for binding in LEVEL_BINDINGS
)

COMPANY_LEVEL: ScopeLevel = next(
    level for level in ORG_LEVELS if level.code_field == "company_code")

PLANT_LEVEL = ScopeLevel("plant_code", "plant_codes")

#: The ``plant|storage location`` pair, never the bare code — a storage location
#: code is unique only inside its plant, and revision 0021 records what grouping
#: on the bare code did. Not grantable: it is here to be *contained* by a plant
#: scope, and scoping somebody to one shelf of one plant is not something
#: anybody has asked for.
STORAGE_LOCATION_LEVEL = ScopeLevel(
    "storage_location_key", "storage_location_keys", grantable=False)


@dataclass(frozen=True)
class ScopeDimension:
    """One containment chain a data scope can be expressed in."""

    key: str
    #: How a refusal names it. Read by a person, so it reads as a phrase.
    label: str
    #: Shallowest first. Position in this tuple *is* the depth, which is why
    #: depth is meaningless between two dimensions.
    levels: tuple[ScopeLevel, ...]
    #: Which index on :class:`ScopeIndexes` answers containment here.
    index_name: str

    def code_fields(self) -> tuple[str, ...]:
        return tuple(level.code_field for level in self.levels)

    def holds(self, code_field: str) -> bool:
        return code_field in self.code_fields()

    def depth(self, code_field: str) -> int:
        """Position in this chain. Larger is deeper.

        Raises rather than defaulting: a level this dimension does not hold has
        no depth here, and the whole point of the split is that answering ``0``
        for it is how a scope comes to be silently dropped.
        """
        try:
            return self.code_fields().index(code_field)
        except ValueError:
            raise KeyError(
                f"{code_field!r} is not a level of the {self.key} scope dimension"
            ) from None

    def filter_fields(self) -> dict[str, str]:
        return {level.code_field: level.filter_field for level in self.levels}


ORG = ScopeDimension(
    key="org",
    label="the sales hierarchy",
    levels=ORG_LEVELS,
    index_name="master",
)

PLANT = ScopeDimension(
    key="plant",
    label="the plant hierarchy",
    levels=(COMPANY_LEVEL, PLANT_LEVEL, STORAGE_LOCATION_LEVEL),
    index_name="location",
)

#: Every dimension, in the order a refusal should name them.
SCOPE_DIMENSIONS: tuple[ScopeDimension, ...] = (ORG, PLANT)

DIMENSION_BY_KEY: dict[str, ScopeDimension] = {d.key: d for d in SCOPE_DIMENSIONS}


def _build_level_map() -> dict[str, ScopeLevel]:
    """Every level once, checked for two dimensions disagreeing about one.

    ``company_code`` is deliberately in both chains, and the two must be the
    same :class:`ScopeLevel` — a level that set ``company_codes`` on one path
    and something else on the other would apply a scope on one page and not on
    the next.
    """
    levels: dict[str, ScopeLevel] = {}
    for dimension in SCOPE_DIMENSIONS:
        for level in dimension.levels:
            existing = levels.setdefault(level.code_field, level)
            if existing != level:
                raise ValueError(
                    f"scope level {level.code_field!r} is declared two different "
                    f"ways: {existing!r} and {level!r}"
                )
    return levels


LEVEL_BY_CODE_FIELD: dict[str, ScopeLevel] = _build_level_map()

#: Every level a scope may *contain*, org first and then plant.
SCOPE_LEVELS: tuple[str, ...] = tuple(LEVEL_BY_CODE_FIELD)

#: Every level an administrator may *assign*.
GRANTABLE_LEVELS: tuple[str, ...] = tuple(
    code_field for code_field, level in LEVEL_BY_CODE_FIELD.items() if level.grantable
)

#: Level -> the ``ScopeFilters`` attribute it sets, across every dimension.
FILTER_FIELD_BY_LEVEL: dict[str, str] = {
    code_field: level.filter_field
    for code_field, level in LEVEL_BY_CODE_FIELD.items()
}

DIMENSIONS_BY_LEVEL: dict[str, tuple[ScopeDimension, ...]] = {
    code_field: tuple(d for d in SCOPE_DIMENSIONS if d.holds(code_field))
    for code_field in SCOPE_LEVELS
}


def grantable_by_dimension() -> tuple[tuple[ScopeDimension, tuple[str, ...]], ...]:
    """Each dimension and the levels it alone offers an administrator.

    **A level in two dimensions is offered by the first that declares it.**
    ``company_code`` is in both chains, and a form rendering one control per
    dimension would otherwise draw Company twice — two controls setting one key,
    where filling in both leaves an administrator with no way to tell which one
    took effect, and no reason to believe the answer.

    Company belongs to the sales hierarchy control because that is where a
    person looks for it; the plant chain reaches the same companies but nobody
    thinks of a company as a kind of plant.
    """
    claimed: set[str] = set()
    result: list[tuple[ScopeDimension, tuple[str, ...]]] = []
    for dimension in SCOPE_DIMENSIONS:
        levels = tuple(
            level.code_field for level in dimension.levels
            if level.grantable and level.code_field not in claimed
        )
        claimed.update(levels)
        if levels:
            result.append((dimension, levels))
    return tuple(result)


def dimensions_of(code_field: str) -> tuple[ScopeDimension, ...]:
    """Every dimension holding this level — two, for ``company_code``."""
    return DIMENSIONS_BY_LEVEL.get(code_field, ())


def exclusive_levels(dimension: ScopeDimension) -> tuple[str, ...]:
    """The levels that belong to this chain and to no other.

    ``company_code`` is in both chains, so its presence on a view says nothing
    about which chain that view is built on — every one of them carries it.
    Only a level unique to a chain identifies it.
    """
    return tuple(
        level.code_field for level in dimension.levels
        if len(dimensions_of(level.code_field)) == 1
    )


def dimension_applies(dimension: ScopeDimension, columns) -> bool:
    """Whether this dataset has this chain at all, whatever it exposes of it.

    **The distinction a scope check turns on, and it is not "does the view carry
    the caller's level".** Two datasets can both lack a ``region_code`` column
    for opposite reasons:

    * A **sale states no plant**. There is no plant behind a sales row to be
      hidden, so a plant scope excludes nothing there and discarding it widens
      nothing. Refusing a sales question because the caller also holds a plant
      scope denies them figures their organisational scope entitles them to in
      full — which is exactly what an account holding a region *and* a plant is.
    * A **credit invoice has a region**, through its customer's sub-territory;
      the view simply does not join that far up (see
      ``reporting.credit.SCOPE_LEVELS_HONOURED``). Dropping a region scope there
      would serve invoices from customers outside it, so it must be refused.

    Both are "the column is missing". What tells them apart is whether the
    dataset has the chain **at all**, and a view answers that by carrying some
    level unique to it: sales carries ``sub_territory_code`` and no plant level,
    credit carries both, material stock carries a plant level and nothing
    organisational below company — which is the same statement revision 0016
    makes about the source.

    Derived from the view's own columns rather than declared per view, so it
    cannot drift from what the view actually carries.
    """
    return any(level in columns for level in exclusive_levels(dimension))


def constrained_dimensions(
    scope: Mapping[str, Sequence[str]],
) -> tuple[ScopeDimension, ...]:
    """The dimensions a scope actually says something about.

    A dimension absent from here **abstains** from every containment test rather
    than denying it: a region-scoped reader's scope makes no claim about which
    plants they may see, and reading that silence as a refusal would take the
    stock page away from them.
    """
    return tuple(
        dimension for dimension in SCOPE_DIMENSIONS
        if any(dimension.holds(level) and codes for level, codes in scope.items())
    )


def scope_in(
    scope: Mapping[str, Sequence[str]], dimension: ScopeDimension,
) -> dict[str, list[str]]:
    """The part of a scope belonging to one dimension."""
    return {
        level: list(codes) for level, codes in scope.items()
        if codes and dimension.holds(level)
    }


def unknown_levels(scope: Mapping[str, Sequence[str]]) -> tuple[str, ...]:
    """Scope keys no dimension declares — a typo, or a name that outlived its level."""
    return tuple(sorted(level for level in scope if level not in LEVEL_BY_CODE_FIELD))


class LocationIndex:
    """Where stock is held: ``company -> plant -> storage location``.

    Its own two queries rather than a reading of ``MasterDataIndex.plants`` and
    ``.storage_locations``, whose keys are the *joined* pairs ``company|plant``
    and ``plant|storage location``. Splitting those apart would reconstruct by
    string surgery what the masters state in two plain columns, and would tie
    containment to the key encoding; both tables are tens of rows, so reading
    the columns costs nothing worth saving.

    Shaped like :class:`app.etl.mapping.MasterDataIndex` on purpose —
    ``parents`` and ``ancestors_of`` mean the same things there — so a caller can
    hold either without knowing which chain it is walking.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        #: level -> every code the master holds at it.
        self.codes: dict[str, set[str]] = {}
        #: level -> {code: parent code}. ``None`` where the parent is unknown
        #: *or* ambiguous; see below.
        self.parents: dict[str, dict[str, str | None]] = {}
        #: Plant codes the master gives more than one company. Kept so a caller
        #: can say why containment declined rather than only that it did.
        self.ambiguous: dict[str, set[str]] = {}
        self._load()

    def _load(self) -> None:
        # A plant code identifies a plant *within* a company (``dim_plant`` is
        # unique on ``plant_key``, not on ``plant_code``), so the same code may
        # in principle name two plants. Where it does, this records **no**
        # parent: choosing one of the two companies would place a plant inside a
        # company that may not own it, and the platform's rule — set out for
        # ``resolve_ancestors`` — is that a parent is derived only where the
        # master states it unambiguously.
        companies: dict[str, set[str]] = {}
        for plant_code, company_code in self.session.execute(
            select(DimPlant.plant_code, DimPlant.company_code)
        ).all():
            if plant_code:
                companies.setdefault(plant_code, set()).add(company_code)

        self.codes["company_code"] = {
            company for owners in companies.values() for company in owners if company
        }
        self.codes["plant_code"] = set(companies)
        self.parents["company_code"] = {
            company: None for company in self.codes["company_code"]
        }
        self.parents["plant_code"] = {
            plant: (next(iter(owners)) if len(owners) == 1 else None)
            for plant, owners in companies.items()
        }
        self.ambiguous = {
            plant: owners for plant, owners in companies.items() if len(owners) > 1
        }

        rows = self.session.execute(
            select(DimStorageLocation.storage_location_key,
                   DimStorageLocation.plant_code)
        ).all()
        self.codes["storage_location_key"] = {key for key, _ in rows if key}
        self.parents["storage_location_key"] = {
            key: plant for key, plant in rows if key
        }

    def ancestors_of(self, level: str, code: str) -> dict[str, str]:
        """Every ancestor code of ``code``, keyed by level. Includes ``level``.

        The same contract as ``MasterDataIndex.ancestors_of``, including that an
        unknown code yields a chain of just itself rather than an error: a
        containment test on a code the master has never heard of should come
        back "not inside your scope", not break the request.
        """
        chain: dict[str, str] = {level: code}
        current_level, current_code = level, code
        while True:
            parent_level = _LOCATION_PARENT.get(current_level)
            if parent_level is None:
                break
            parent_code = self.parents.get(current_level, {}).get(current_code)
            if not parent_code:
                break
            chain[parent_level] = parent_code
            current_level, current_code = parent_level, parent_code
        return chain

    def has(self, level: str, code: str) -> bool:
        return code in self.codes.get(level, set())


#: Level -> its parent level, in the plant chain. Derived from ``PLANT`` so that
#: the dimension stays the single statement of the order.
_LOCATION_PARENT: dict[str, str] = {
    child.code_field: parent.code_field
    for parent, child in zip(PLANT.levels, PLANT.levels[1:])
}


class ScopeIndexes:
    """Both masters a scope check may need, built lazily from one session.

    Lazily because the two cost very different amounts: ``MasterDataIndex``
    loads every dimension for the ETL's benefit, while ``LocationIndex`` is two
    small selects. A request that only asks about plants should not pay for the
    other, and a request that never asks about plants should not pay for this.
    """

    def __init__(self, session: Session, *,
                 master: MasterDataIndex | None = None,
                 location: LocationIndex | None = None) -> None:
        self.session = session
        self._master = master
        self._location = location

    @property
    def master(self) -> MasterDataIndex:
        if self._master is None:
            self._master = MasterDataIndex(self.session)
        return self._master

    @property
    def location(self) -> LocationIndex:
        if self._location is None:
            self._location = LocationIndex(self.session)
        return self._location

    def of(self, dimension: ScopeDimension):
        return getattr(self, dimension.index_name)

    def ancestors_of(self, dimension: ScopeDimension, level: str,
                     code: str) -> dict[str, str]:
        """The chain above ``code`` **within one dimension**.

        Per dimension rather than merged, because that is the shape the
        containment rule needs: each dimension answers for itself and the
        answers are combined by the caller, never averaged here.
        """
        if not dimension.holds(level):
            return {}
        return self.of(dimension).ancestors_of(level, code)

    def exists(self, level: str, code: str) -> bool:
        """Whether any dimension's master holds this code at this level.

        The check an administrator's scope assignment needs: granting access to
        a plant that does not exist behaves as "no access" and is very hard to
        diagnose, which is the reason ``_validate_scope`` has always made it.
        """
        for dimension in dimensions_of(level):
            index = self.of(dimension)
            if isinstance(index, LocationIndex):
                if index.has(level, code):
                    return True
            elif code in index.ids.get(level, {}):
                return True
        return False


__all__ = [
    "ScopePolicy",
    "level_label",
    "describe_levels",
    "ScopeLevel",
    "ScopeDimension",
    "ORG",
    "PLANT",
    "SCOPE_DIMENSIONS",
    "DIMENSION_BY_KEY",
    "ORG_LEVELS",
    "COMPANY_LEVEL",
    "PLANT_LEVEL",
    "STORAGE_LOCATION_LEVEL",
    "LEVEL_BY_CODE_FIELD",
    "SCOPE_LEVELS",
    "GRANTABLE_LEVELS",
    "FILTER_FIELD_BY_LEVEL",
    "DIMENSIONS_BY_LEVEL",
    "dimensions_of",
    "exclusive_levels",
    "dimension_applies",
    "grantable_by_dimension",
    "constrained_dimensions",
    "scope_in",
    "unknown_levels",
    "LocationIndex",
    "ScopeIndexes",
]
