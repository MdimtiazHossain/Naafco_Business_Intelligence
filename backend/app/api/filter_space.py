"""What each filter may still offer, given everything else that is selected.

The filter bar used to ask one question per control: *what sits under the
nearest ancestor I have selected?* That question has a wrong answer whenever the
nearest selected ancestor is not the **immediate** parent. Choosing a company
and opening Territory asked ``dim_territory`` for the rows whose ``unit_code``
equals a company code, which is nothing at all — 0 options against the 154
territories that company actually has — so the control drew an empty list and
could not be used. Business Unit appeared to work only because company happens
to be its immediate parent.

This module asks a different question, once, for every control at the same time:
**given everything currently selected, which values does the master data still
allow here?** A level is therefore never gated on its parent being chosen; it is
gated only on a value existing that is consistent with the rest of the
selection. That is what makes Company + Territory, Company + Sub-Territory and
Territory-then-Company all work without walking the chain in between.

Three spaces, because master data has three independent structures
--------------------------------------------------------------------

* **org** — Company -> Business Unit -> Sales Line -> Zone -> Region -> Area ->
  Unit -> Territory -> Sub-Territory, plus Customer (through
  ``dim_customer.sub_territory_code``) and Sales Force (through
  ``dim_sales_force.territory_code``) hanging off the level each names.
* **plant** — Company -> Plant -> Storage Location, which only a stock position
  has.
* **material** — Company -> Material Group -> Material Brand -> Material, all
  four off ``dim_material``'s own row.

A *row* in a space is one path through it, and a row **only matches a filter on
a level it actually carries**. A row built from a company alone does not answer
for a territory, so selecting a territory cannot leave every company standing.

``company_code`` is the one level all three spaces share, and it is what lets
them constrain each other: choosing a territory narrows the companies the org
space allows, and those companies then narrow the plant and material lists. The
propagation is deliberately one hop through that shared level rather than a
join across spaces — no master relates a territory to a plant, and inventing
one would be the guess this system exists to avoid.

Two rules keep the intersection honest
--------------------------------------

* **A filter never restricts itself** (the specification's §8). Territory's
  options are computed from every *other* selected level, so choosing three
  territories cannot make the other 151 unreachable.
* **An unconstrained space contributes nothing.** A space only narrows the
  shared company when it has a selection of its own; otherwise a company with
  plants but no materials would vanish from the Company list for no reason. And
  a space whose matching rows record no company at all says "unknown" and is
  skipped, rather than saying "no company" and emptying every other control.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.entity_resolver import ENTITY_BINDINGS
from ..ai.permission_filter import PermissionFilter
from ..database.models import DimMaterial, DimPlant, DimStorageLocation
from ..database.models_warehouse import DimCustomer, DimSalesForce
from ..etl.mapping import BINDING_BY_LEVEL, LEVEL_BINDINGS, MasterDataIndex

#: The nine organisational levels, shallowest first — derived, never restated.
ORG_LEVELS: tuple[str, ...] = tuple(b.code_field for b in LEVEL_BINDINGS)

#: The two masters that hang off an organisational level rather than being one.
ATTACHED_LEVELS: tuple[str, ...] = ("customer_code", "sales_force_code")

ORG_SPACE_LEVELS: tuple[str, ...] = (*ORG_LEVELS, *ATTACHED_LEVELS)
PLANT_SPACE_LEVELS: tuple[str, ...] = (
    "company_code", "plant_code", "storage_location_key",
)
MATERIAL_SPACE_LEVELS: tuple[str, ...] = (
    "company_code", "material_group_code", "material_brand", "material_code",
)

#: The level every space carries, and so the only one through which one space
#: can narrow another.
SHARED_LEVEL = "company_code"

#: Every level this module can answer for. A level outside it — ``batch_code``,
#: which is transaction data, or ``expiry_status``, which is derived from a date
#: — has no master list to intersect and is left alone by the caller.
SPACE_LEVELS: frozenset[str] = frozenset(
    (*ORG_SPACE_LEVELS, *PLANT_SPACE_LEVELS, *MATERIAL_SPACE_LEVELS)
)

#: How many options one level may return.
MAX_OPTIONS = 500

#: Bind parameters per ``IN`` chunk. Well under SQLite's 32,766 ceiling, which
#: the rest of the codebase chunks against for the same reason.
_CHUNK = 900

#: Level -> the entity binding that names its master's code and name columns.
_ENTITY_BY_LEVEL = {b.code_field: b for b in ENTITY_BINDINGS}


def _chunks(values: Sequence[str], size: int = _CHUNK) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _snapshot(selection: Mapping[str, Sequence[str]]) -> tuple:
    """A hashable, order-independent view of one selection, for memoising."""
    return tuple(sorted(
        (level, tuple(sorted(set(values))))
        for level, values in selection.items() if values
    ))


def _matches(row: Mapping[str, str], constraints: Mapping[str, set[str]]) -> bool:
    """True when this path satisfies every constraint.

    A row that does not carry a constrained level does **not** match. That is
    the rule that makes the intersection work in both directions: the row built
    from company ``1000`` alone carries no territory, so a territory filter
    rules it out instead of passing straight through it.
    """
    for level, values in constraints.items():
        value = row.get(level)
        if value is None or value not in values:
            return False
    return True


@dataclass
class _Space:
    """One structure of the master data, as the set of paths through it."""

    levels: frozenset[str]
    rows: list[dict[str, str]] = field(default_factory=list)


@dataclass
class LevelOptions:
    """One control's worth of options, already sorted and capped."""

    level: str
    total: int
    truncated: bool
    options: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "total": self.total,
            "truncated": self.truncated,
            "options": self.options,
        }


class FilterSpace:
    """The master data as three intersectable spaces, built once per request.

    Every table read here is small — 267 sub-territories, 405 materials, 47
    plants, 46 storage locations — so the whole space is a few thousand dicts
    and the intersection is done in memory. The alternative, a query per control
    per keystroke, is what the old cascade did and it still could not answer the
    question correctly.

    ``dim_customer`` is the one table worth not reading: it holds thousands of
    rows and only matters when a customer is selected or asked about, so it is
    loaded on demand rather than on every dashboard filter change.
    """

    def __init__(self, session: Session, *, with_customers: bool = False) -> None:
        self.session = session
        self.index = MasterDataIndex(session)
        self._labels: dict[str, dict[str, str]] = {}
        # One control's options need the shared-company domain, and a bar has
        # fifteen controls asking about the same selection. The domain varies
        # only with the selection and with which level is excluding itself, so
        # it is computed once per pair rather than once per control.
        self._domains: dict[tuple[Any, str], set[str] | None] = {}
        self._org = _Space(frozenset(ORG_SPACE_LEVELS),
                           self._org_rows(with_customers=with_customers))
        self._plant = _Space(frozenset(PLANT_SPACE_LEVELS), self._plant_rows())
        self._material = _Space(frozenset(MATERIAL_SPACE_LEVELS),
                                self._material_rows())
        self._spaces = (self._org, self._plant, self._material)

    # -- construction -------------------------------------------------------

    def _org_rows(self, *, with_customers: bool) -> list[dict[str, str]]:
        """One path per master record, at every level.

        Built from *every* level rather than from the deepest one, so a company
        that has no territories beneath it is still a company the bar can offer.
        ``ancestors_of`` fills in everything above the level; everything below
        stays unset, which is exactly what "this row cannot answer for a
        territory" has to look like.
        """
        rows: list[dict[str, str]] = []
        for level in ORG_LEVELS:
            for code in self.index.ids.get(level, {}):
                if code:
                    rows.append(dict(self.index.ancestors_of(level, code)))
        if with_customers:
            rows.extend(self._attached_rows())
        return rows

    def _attached_rows(self) -> list[dict[str, str]]:
        """Customers and sales-force members, on the path of the level they name.

        A customer is not an organisational level, but it does have one
        authoritative parent — ``dim_customer.sub_territory_code``, revision
        0011 — so putting it on that sub-territory's path is what lets a chosen
        customer narrow all nine levels above it and a chosen zone narrow the
        customer list, from the same rows.
        """
        rows: list[dict[str, str]] = []
        for code, sub in self.session.execute(
            select(DimCustomer.customer_code, DimCustomer.sub_territory_code)
            .where(DimCustomer.is_deleted.is_(False))
        ):
            if not code:
                continue
            path = dict(self.index.ancestors_of("sub_territory_code", sub)) if sub else {}
            path["customer_code"] = code
            rows.append(path)
        for code, territory in self.session.execute(
            select(DimSalesForce.sales_force_code, DimSalesForce.territory_code)
            .where(DimSalesForce.is_deleted.is_(False))
        ):
            if not code:
                continue
            path = (dict(self.index.ancestors_of("territory_code", territory))
                    if territory else {})
            path["sales_force_code"] = code
            rows.append(path)
        return rows

    def _plant_rows(self) -> list[dict[str, str]]:
        """Company -> Plant -> Storage Location.

        A plant code is unique within a company rather than globally, so the
        same code can belong to two companies; a storage location names only its
        plant code, so it is emitted once per company that plant code appears
        under. Choosing one arbitrarily would silently attach a location to a
        company it may not be in.
        """
        rows: list[dict[str, str]] = []
        companies_of_plant: dict[str, set[str]] = {}
        for company, plant in self.session.execute(
            select(DimPlant.company_code, DimPlant.plant_code)
            .where(DimPlant.is_deleted.is_(False))
        ):
            if not plant:
                continue
            row: dict[str, str] = {"plant_code": plant}
            if company:
                row[SHARED_LEVEL] = company
                companies_of_plant.setdefault(plant, set()).add(company)
            rows.append(row)

        for key, plant in self.session.execute(
            select(DimStorageLocation.storage_location_key,
                   DimStorageLocation.plant_code)
            .where(DimStorageLocation.is_deleted.is_(False))
        ):
            if not key:
                continue
            if not plant:
                rows.append({"storage_location_key": key})
                continue
            companies = companies_of_plant.get(plant)
            if not companies:
                rows.append({"storage_location_key": key, "plant_code": plant})
                continue
            for company in companies:
                rows.append({"storage_location_key": key, "plant_code": plant,
                             SHARED_LEVEL: company})
        return rows

    def _material_rows(self) -> list[dict[str, str]]:
        """Company -> Material Group -> Material Brand -> Material, one row each.

        Group and brand are columns of the material rather than tables of their
        own, so the material rows *are* the paths: every group and every brand
        that exists is on at least one of them. A material whose company the
        master does not state keeps no company, which means a company filter
        excludes it rather than claiming it for whichever company was chosen.
        """
        rows: list[dict[str, str]] = []
        for company, group, brand, code in self.session.execute(
            select(DimMaterial.company_code, DimMaterial.material_group_code,
                   DimMaterial.material_brand, DimMaterial.material_code)
            .where(DimMaterial.is_deleted.is_(False))
        ):
            row = {
                level: value for level, value in (
                    (SHARED_LEVEL, company),
                    ("material_group_code", group),
                    ("material_brand", brand),
                    ("material_code", code),
                ) if value
            }
            if row:
                rows.append(row)
        return rows

    # -- intersection -------------------------------------------------------

    def _space_of(self, level: str) -> _Space:
        for space in self._spaces:
            if level in space.levels:
                return space
        raise KeyError(level)

    @staticmethod
    def _constraints(space: _Space, selection: Mapping[str, Sequence[str]],
                     *exclude: str) -> dict[str, set[str]]:
        return {
            level: set(values)
            for level, values in selection.items()
            if values and level in space.levels and level not in exclude
        }

    def _company_domain(self, selection: Mapping[str, Sequence[str]],
                        target_level: str) -> set[str] | None:
        """The companies still consistent with the selection, or ``None``.

        ``None`` means *unconstrained* — no space had anything to say — which is
        different from the empty set, meaning the selection genuinely allows no
        company at all. The distinction matters: a space with no selection of
        its own must not narrow anything, or a company with plants but no
        materials would disappear from the Company list for no reason.

        The explicit company selection seeds the domain, except when Company is
        the control being computed: a filter never restricts itself.
        """
        key = (_snapshot(selection), target_level)
        if key in self._domains:
            return self._domains[key]
        domain = self._compute_company_domain(selection, target_level)
        self._domains[key] = domain
        return domain

    def _compute_company_domain(self, selection: Mapping[str, Sequence[str]],
                                target_level: str) -> set[str] | None:
        domain: set[str] | None = None
        if target_level != SHARED_LEVEL and selection.get(SHARED_LEVEL):
            domain = set(selection[SHARED_LEVEL])

        for space in self._spaces:
            constraints = self._constraints(space, selection,
                                            SHARED_LEVEL, target_level)
            if not constraints:
                continue
            matched = [row for row in space.rows if _matches(row, constraints)]
            candidates = {row[SHARED_LEVEL] for row in matched
                          if row.get(SHARED_LEVEL)}
            # Rows exist but none records a company: this space knows these
            # values and does not know where they belong. Saying "no company"
            # would empty every other control on an absence of information.
            if matched and not candidates:
                continue
            domain = candidates if domain is None else domain & candidates
        return domain

    def codes_for(self, level: str,
                  selection: Mapping[str, Sequence[str]]) -> set[str]:
        """Every value this level may still offer under the current selection."""
        if level not in SPACE_LEVELS:
            return set()

        domain = self._company_domain(selection, level)

        if level == SHARED_LEVEL:
            known = {code for code in self.index.ids.get(SHARED_LEVEL, {}) if code}
            return known if domain is None else known & domain

        space = self._space_of(level)
        constraints = self._constraints(space, selection, level, SHARED_LEVEL)
        codes: set[str] = set()
        for row in space.rows:
            value = row.get(level)
            if value is None:
                continue
            if domain is not None and row.get(SHARED_LEVEL) not in domain:
                continue
            if _matches(row, constraints):
                codes.add(value)
        return codes

    # -- labels -------------------------------------------------------------

    def labels_for(self, level: str) -> dict[str, str]:
        """``code -> label`` for one level's master, read once per request."""
        cached = self._labels.get(level)
        if cached is None:
            cached = self._load_labels(level)
            self._labels[level] = cached
        return cached

    def _load_labels(self, level: str) -> dict[str, str]:
        if level == "storage_location_key":
            # Qualified by the plant, exactly as ``storage_location_label`` on
            # the stock view is: six locations are called "FG Store" and the
            # name alone cannot tell a user which one they picked.
            rows = self.session.execute(
                select(DimStorageLocation.storage_location_key,
                       DimStorageLocation.storage_location_name,
                       DimPlant.plant_name,
                       DimStorageLocation.plant_code)
                .join(DimPlant,
                      DimPlant.plant_code == DimStorageLocation.plant_code,
                      isouter=True)
                .where(DimStorageLocation.is_deleted.is_(False))
            ).all()
            return {
                key: f"{name or key} - {plant_name or plant_code or ''}".strip(" -")
                for key, name, plant_name, plant_code in rows if key
            }

        if level == "plant_code":
            rows = self.session.execute(
                select(DimPlant.plant_code, DimPlant.plant_name)
                .where(DimPlant.is_deleted.is_(False))
            ).all()
            return {code: name or code for code, name in rows if code}

        if level == "material_group_code":
            rows = self.session.execute(
                select(DimMaterial.material_group_code,
                       DimMaterial.material_group_name)
                .where(DimMaterial.is_deleted.is_(False)).distinct()
            ).all()
            return {code: name or code for code, name in rows if code}

        if level == "material_brand":
            # The name *is* the identity: ``material_brand_code`` is ``'0'`` on
            # every material, so it distinguishes nothing.
            return {}

        binding = _ENTITY_BY_LEVEL.get(level)
        if binding is None or binding.name_field is None:
            return {}
        rows = self.session.execute(
            select(getattr(binding.model, binding.code_field),
                   getattr(binding.model, binding.name_field))
        ).all()
        return {code: name or code for code, name in rows if code}

    # -- the answer ---------------------------------------------------------

    def options_for(self, level: str, selection: Mapping[str, Sequence[str]],
                    permissions: PermissionFilter | None = None,
                    search: str | None = None,
                    limit: int = 200) -> LevelOptions:
        """One control's options: intersected, scoped, searched, sorted, capped."""
        codes = self.codes_for(level, selection)

        # The same scope check the agent and the reports use, so a filter can
        # never offer a value whose report would be refused.
        if permissions is not None and level in BINDING_BY_LEVEL:
            codes = {code for code in codes
                     if permissions.is_within_scope(level, code)}

        labels = self.labels_for(level)
        entries = [{"code": code, "label": labels.get(code) or code,
                    "parent_code": None} for code in codes]

        if search:
            needle = search.strip().lower()
            entries = [entry for entry in entries
                       if needle in entry["code"].lower()
                       or needle in str(entry["label"]).lower()]

        entries.sort(key=lambda entry: (str(entry["label"]).lower(),
                                        str(entry["code"])))
        capped = min(limit, MAX_OPTIONS)
        return LevelOptions(level=level, total=len(entries),
                            truncated=len(entries) > capped,
                            options=entries[:capped])

    def prune(self, selection: Mapping[str, Sequence[str]],
              anchor: Sequence[str] = ()) -> tuple[dict[str, list[str]],
                                                   dict[str, list[str]]]:
        """Drop the selected values the rest of the selection no longer allows.

        The specification's §5: changing a parent keeps what is still valid and
        removes only what is not. A territory that exists under the newly chosen
        company survives; one that does not is taken off, and every other filter
        carries on.

        ``anchor`` names the levels the user just changed. They are **never**
        pruned, and they are what makes the answer well defined: with Company
        changed from A to B while territory T-001 lives only under A, either
        "drop T-001" or "drop B" satisfies the data, and only the user's last
        action says which one they meant. Everything else is validated against
        the anchor and against the values kept so far, shallowest level first,
        so a pruned parent cannot go on invalidating its children.
        """
        anchored = set(anchor)
        kept: dict[str, list[str]] = {
            level: list(values) for level, values in selection.items()
            if values and (level in anchored or level not in SPACE_LEVELS)
        }
        removed: dict[str, list[str]] = {}

        ordered = [level for level in _PRUNE_ORDER
                   if level in selection and level not in kept and selection[level]]
        for level in ordered:
            allowed = self.codes_for(level, kept)
            surviving = [value for value in selection[level] if value in allowed]
            dropped = [value for value in selection[level] if value not in allowed]
            if surviving:
                kept[level] = surviving
            if dropped:
                removed[level] = dropped
        return kept, removed


#: The order :meth:`FilterSpace.prune` validates in: shallowest organisational
#: level first, then the two attached masters, then the plant and item chains.
#: A parent settles before its children are judged against it, so one removal
#: cannot cascade through a level that was itself about to be removed.
_PRUNE_ORDER: tuple[str, ...] = (
    *ORG_LEVELS,
    *ATTACHED_LEVELS,
    "plant_code",
    "storage_location_key",
    "material_group_code",
    "material_brand",
    "material_code",
)


__all__ = [
    "FilterSpace",
    "LevelOptions",
    "MAX_OPTIONS",
    "ORG_LEVELS",
    "SPACE_LEVELS",
    "SHARED_LEVEL",
]
