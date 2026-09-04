"""The levels a map layer may draw, and where each one's records live.

**Derived, not restated.** The organisational levels come from
:func:`app.org.hierarchy.org_level`, which is the same wiring the permission
scope, Data Management and the ETL read — so a level added to the chain becomes
drawable without a second list to keep in step, and the map cannot disagree
with the warehouse about which table a territory lives in.

Two levels are not rungs of the organisational ladder and are declared here:
**customer**, attached to its sub-territory through ``dim_customer``'s own
column, and **sales force**, attached to its territory the same way. They are
the levels a real address exists for, and everything above them is derived as
a centroid of what is placed below (:func:`app.map.geo.derive_parents`).

The specification's six levels — Zone, Region, Area, Territory, Sub-Territory,
Customer — are all here, plus Unit, which sits between Area and Territory in
this platform's chain and cannot be skipped without breaking the parent walk.
Company, business unit and sales line are drawable too but not promoted: a
company-level point is one dot for the whole country.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..database.models_map import LayerViewMode
from ..database.models_warehouse import DimCustomer, DimSalesForce
from ..org.hierarchy import BUSINESS_TYPES, ORG_CHAIN, org_level

#: Levels the layer editor offers by default, in the order the specification
#: lists them. Every other level stays drawable but is not promoted.
PROMOTED_LEVELS: tuple[str, ...] = (
    "zone", "region", "area", "unit", "territory", "sub_territory", "customer",
)

GROUP_ORGANISATION = "Organisation"
GROUP_BUSINESS = "Business"

#: Human labels for the hierarchy levels. Derived keys are ugly (``bu``), and
#: the label is what an administrator picks from.
_LABELS: dict[str, str] = {
    "company": "Company",
    "bu": "Business Unit",
    "sales_line": "Sales Line",
    "zone": "Zone",
    "region": "Region",
    "area": "Area",
    "unit": "Unit",
    "territory": "Territory",
    "sub_territory": "Sub-Territory",
    "customer": "Customer",
    "sales_force": "Sales Force",
}


@dataclass(frozen=True)
class MapLevel:
    """One kind of thing the map can draw."""

    key: str
    label: str
    #: The ORM model whose rows are the entities.
    model: Any
    #: Business-code column identifying an instance, e.g. ``region_code``.
    code_field: str
    #: Display-name column, e.g. ``region_name``.
    name_field: str
    #: The level above, and the column on this level's row that names it.
    parent: str | None
    parent_code_field: str | None
    group: str
    #: Depth in the organisational hierarchy; ``None`` for the business levels.
    depth: int | None = None
    #: Where this level's outlines come from, when anything states them.
    #:
    #: Nothing does today. ``Master Data.xlsx`` carries no geometry, and the
    #: administrative boundaries this platform once drew are divisions and
    #: districts, which a sales region is not — a business region has no
    #: outline anyone has stated, and this platform does not draw one it was
    #: not given. A level gains one by naming its source here; until then the
    #: layer editor offers Point alone, so Boundary and Both are absent rather
    #: than inert.
    boundary_source: str | None = None

    @property
    def promoted(self) -> bool:
        return self.key in PROMOTED_LEVELS

    @property
    def boundary_available(self) -> bool:
        return self.boundary_source is not None

    @property
    def table(self) -> str:
        return self.model.__tablename__

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "code_field": self.code_field,
            "name_field": self.name_field,
            "table": self.table,
            "parent": self.parent,
            "group": self.group,
            "depth": self.depth,
            "promoted": self.promoted,
            "boundary_available": self.boundary_available,
            "view_modes": list(view_modes_for(self)),
        }


#: How a layer may draw its entities, as the layer editor offers the choice.
#:
#: ``requires_boundary`` is what keeps Boundary and Both off a level that has
#: no outline to draw. The mode is still stored per layer (``map_layers.
#: view_mode``) so a design can say what it wants; what it is *offered* is what
#: the level can honour today.
VIEW_MODES: tuple[dict[str, Any], ...] = (
    {"key": LayerViewMode.POINT, "label": "Point", "requires_boundary": False},
    {"key": LayerViewMode.BOUNDARY, "label": "Boundary", "requires_boundary": True},
    {"key": LayerViewMode.BOTH, "label": "Both", "requires_boundary": True},
)


def view_modes_for(level: "MapLevel") -> tuple[str, ...]:
    """The view modes a layer at this level may name."""
    return tuple(
        mode["key"] for mode in VIEW_MODES
        if not mode["requires_boundary"] or level.boundary_available
    )


def _organisational() -> tuple[MapLevel, ...]:
    levels = []
    for depth, key in enumerate(ORG_CHAIN):
        model, code, name, parent_code = org_level(key)
        levels.append(MapLevel(
            key=key,
            label=_LABELS.get(key, key.replace("_", " ").title()),
            model=model, code_field=code, name_field=name,
            parent=parent_code.removesuffix("_code") if parent_code else None,
            parent_code_field=parent_code,
            group=GROUP_ORGANISATION, depth=depth,
        ))
    return tuple(levels)


#: The two levels resolved from their own dimension's parent column rather
#: than from the chain. Keyed the way :data:`app.org.hierarchy.BUSINESS_TYPES`
#: names them, and asserted against it below so the two cannot drift.
_BUSINESS: tuple[MapLevel, ...] = (
    MapLevel(
        key="customer", label=_LABELS["customer"], model=DimCustomer,
        code_field="customer_code", name_field="customer_name",
        parent="sub_territory", parent_code_field="sub_territory_code",
        group=GROUP_BUSINESS,
    ),
    MapLevel(
        key="sales_force", label=_LABELS["sales_force"], model=DimSalesForce,
        code_field="sales_force_code", name_field="sales_force_name",
        parent="territory", parent_code_field="territory_code",
        group=GROUP_BUSINESS,
    ),
)

assert tuple(level.key for level in _BUSINESS) == BUSINESS_TYPES, (
    "the map's business levels must be the hierarchy's business types"
)

MAP_LEVELS: tuple[MapLevel, ...] = (*_organisational(), *_BUSINESS)
LEVEL_BY_KEY: dict[str, MapLevel] = {level.key: level for level in MAP_LEVELS}
LEVEL_KEYS: tuple[str, ...] = tuple(LEVEL_BY_KEY)


def get_level(key: str) -> MapLevel:
    """Look up a level, raising a clear error for an unknown key."""
    level = LEVEL_BY_KEY.get(key)
    if level is None:
        raise ValueError(
            f"Unknown map level {key!r}. Supported: {', '.join(LEVEL_KEYS)}."
        )
    return level


def catalogue() -> list[dict[str, Any]]:
    """Every level as the layer editor's dropdown consumes it."""
    return [level.to_dict() for level in MAP_LEVELS]


__all__ = [
    "GROUP_BUSINESS",
    "GROUP_ORGANISATION",
    "LEVEL_BY_KEY",
    "LEVEL_KEYS",
    "MAP_LEVELS",
    "MapLevel",
    "PROMOTED_LEVELS",
    "VIEW_MODES",
    "catalogue",
    "get_level",
    "view_modes_for",
]
