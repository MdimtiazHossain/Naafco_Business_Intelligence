"""What a marker can be assigned to.

The organisational entity types are **derived** from
:data:`app.etl.mapping.LEVEL_BINDINGS` — the same hierarchy the warehouse, the
permission scope and the ETL already use — so a level added to the hierarchy
becomes assignable without a second list to keep in step.

Two further types (customer, sales force) come from the Phase 2 future-ready
dimensions. They are assignable now even though those dimensions are still
``PENDING_SOURCE_DATA``: a design is configuration, and configuring it before the
data lands is exactly the point.

Adding an entity type is one entry in :data:`EXTRA_ENTITY_TYPES` — nothing else
in the designer, the API or the resolver needs to change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..etl.mapping import LEVEL_BINDINGS

#: Entity types the *designer* offers by default, in the order the spec lists
#: them. Levels above zone remain assignable but are not promoted in the UI,
#: because a company-level marker is rarely what an operator wants to draw.
PROMOTED_KEYS: tuple[str, ...] = (
    "zone", "region", "area", "unit", "territory", "sub_territory",
    "customer", "sales_force",
)


@dataclass(frozen=True)
class EntityType:
    """One thing on the map that can carry a marker design."""

    key: str
    label: str
    #: Business-code column identifying an instance, e.g. ``region_code``.
    code_field: str
    #: Dimension table the codes live in, for validating a specific assignment.
    table: str
    group: str
    #: Depth in the organisational hierarchy; ``None`` for non-hierarchy types.
    depth: int | None = None
    #: False while the dimension has no source data yet — surfaced in the UI so
    #: an administrator understands why a code list is empty.
    has_source_data: bool = True
    description: str = ""

    @property
    def promoted(self) -> bool:
        return self.key in PROMOTED_KEYS

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "code_field": self.code_field,
            "table": self.table,
            "group": self.group,
            "depth": self.depth,
            "has_source_data": self.has_source_data,
            "promoted": self.promoted,
            "description": self.description,
        }


GROUP_HIERARCHY = "Organisation"
GROUP_OPERATIONS = "Operations"

#: Human labels for the hierarchy levels. Derived keys are ugly (``bu``), and
#: the label is what an administrator picks from.
_HIERARCHY_LABELS = {
    "company": "Company",
    "bu": "Business Unit",
    "sales_line": "Sales Line",
    "zone": "Zone",
    "region": "Region",
    "area": "Area",
    "unit": "Unit",
    "territory": "Territory",
    "sub_territory": "Sub-Territory",
}


def _hierarchy_types() -> tuple[EntityType, ...]:
    """One entity type per organisational level, straight from the bindings."""
    types = []
    for depth, binding in enumerate(LEVEL_BINDINGS):
        key = binding.code_field.removesuffix("_code")
        types.append(EntityType(
            key=key,
            label=_HIERARCHY_LABELS.get(key, key.replace("_", " ").title()),
            code_field=binding.code_field,
            table=binding.model.__tablename__,
            group=GROUP_HIERARCHY,
            depth=depth,
            description=f"Organisational level {depth + 1} of {len(LEVEL_BINDINGS)}.",
        ))
    return tuple(types)


#: Non-hierarchy entity types. Extend here to make a new thing assignable.
EXTRA_ENTITY_TYPES: tuple[EntityType, ...] = (
    # No warehouse type. The dimension went in revision 0020 and nothing
    # replaced it as a *placeable* thing: stock now sits in a plant and a
    # storage location, and neither master records a coordinate. Adding either
    # here would offer an operator a marker they could never place.
    EntityType(
        key="customer", label="Customer", code_field="customer_code",
        table="dim_customer", group=GROUP_OPERATIONS, has_source_data=False,
        description="Dealers, retailers and institutional customers.",
    ),
    EntityType(
        key="sales_force", label="Sales Force", code_field="sales_force_code",
        table="dim_sales_force", group=GROUP_OPERATIONS, has_source_data=False,
        description="Field sales people and their coverage.",
    ),
)

ENTITY_TYPES: tuple[EntityType, ...] = (*_hierarchy_types(), *EXTRA_ENTITY_TYPES)
ENTITY_TYPE_BY_KEY: dict[str, EntityType] = {e.key: e for e in ENTITY_TYPES}
ENTITY_KEYS: tuple[str, ...] = tuple(ENTITY_TYPE_BY_KEY)


def get_entity_type(key: str) -> EntityType:
    """Look up an entity type, raising a clear error for an unknown key."""
    entity = ENTITY_TYPE_BY_KEY.get(key)
    if entity is None:
        raise ValueError(
            f"Unknown entity type {key!r}. Supported: {', '.join(ENTITY_KEYS)}."
        )
    return entity


def catalogue() -> list[dict[str, Any]]:
    """Entity types as the designer's dropdown consumes them."""
    return [entity.to_dict() for entity in ENTITY_TYPES]


__all__ = [
    "EntityType",
    "ENTITY_TYPES",
    "ENTITY_TYPE_BY_KEY",
    "ENTITY_KEYS",
    "EXTRA_ENTITY_TYPES",
    "PROMOTED_KEYS",
    "GROUP_HIERARCHY",
    "GROUP_OPERATIONS",
    "get_entity_type",
    "catalogue",
]
