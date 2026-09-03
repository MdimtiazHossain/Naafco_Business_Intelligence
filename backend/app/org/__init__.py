"""The organisational hierarchy: Zone, Region, Area, Territory, Sub-Territory.

This is business structure, not geography. It lived under ``app/map/`` only
because the business map was the first surface to need it, which made the map
look like the owner of a chain that Data Management, permission scope and the
reporting filters all depend on just as much. Removing the map made that
accidental ownership visible, so the chain moved here and the map did not take
it with it.
"""

from .hierarchy import (
    ALL_TYPES,
    ORG_CHAIN,
    OrgScope,
    resolve_business_entities,
    resolve_org_scope,
)

__all__ = [
    "ALL_TYPES",
    "ORG_CHAIN",
    "OrgScope",
    "resolve_business_entities",
    "resolve_org_scope",
]
