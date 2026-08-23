"""Master-data mapping: transaction codes -> Phase 1 dimension surrogate keys.

This is where "every transaction must be linked to valid master records" is
enforced. The Phase 1 dimensions are read-only here: nothing is created,
nothing is renamed, no missing master record is invented.

Two distinct checks are performed, and the second is the one that catches the
subtle errors:

1. **Existence** — does ``R001`` exist in ``dim_region``?
2. **Hierarchy** — a row carrying ``Region = R001`` *and* ``Zone = Z002`` is
   rejected when ``R001`` actually sits under ``Z001``. Codes existing is not
   enough; they must belong to the same branch.

A source that carries only the deepest level gets the rest of the hierarchy
derived for it, so a territory-level sales file still rolls up to company.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database.models import (
    DimArea,
    DimBusinessUnit,
    DimCompany,
    DimMaterial,
    DimPlant,
    DimRegion,
    DimSalesLine,
    DimStorageLocation,
    DimSubTerritory,
    DimTerritory,
    DimUnit,
    DimZone,
    plant_key,
    storage_location_key,
)
from ..database.models_warehouse import (
    STATUS_AVAILABLE,
    STATUS_PENDING_SOURCE_DATA,
    DimCustomer,
    DimSalesForce,
    MasterSourceStatus,
)
from . import errors
from .datasets import ORG_LEVELS
from .errors import ErrorSpec


@dataclass(frozen=True)
class LevelBinding:
    """Wiring between an organisational level and its dimension table."""

    code_field: str          # e.g. "region_code"
    dimension_field: str     # e.g. "region_id"
    model: type
    surrogate_key: str
    parent_code_field: str | None


#: Shallowest first; each level knows its parent's code field.
LEVEL_BINDINGS: tuple[LevelBinding, ...] = (
    LevelBinding("company_code", "company_id", DimCompany, "company_id", None),
    LevelBinding("bu_code", "business_unit_id", DimBusinessUnit, "business_unit_id",
                 "company_code"),
    LevelBinding("sales_line_code", "sales_line_id", DimSalesLine, "sales_line_id", "bu_code"),
    LevelBinding("zone_code", "zone_id", DimZone, "zone_id", "sales_line_code"),
    LevelBinding("region_code", "region_id", DimRegion, "region_id", "zone_code"),
    LevelBinding("area_code", "area_id", DimArea, "area_id", "region_code"),
    LevelBinding("unit_code", "unit_id", DimUnit, "unit_id", "area_code"),
    LevelBinding("territory_code", "territory_id", DimTerritory, "territory_id", "unit_code"),
    LevelBinding("sub_territory_code", "sub_territory_id", DimSubTerritory, "sub_territory_id",
                 "territory_code"),
)

BINDING_BY_LEVEL: dict[str, LevelBinding] = {b.code_field: b for b in LEVEL_BINDINGS}
#: Depth index; larger means deeper in the hierarchy.
LEVEL_DEPTH: dict[str, int] = {b.code_field: i for i, b in enumerate(LEVEL_BINDINGS)}


@dataclass
class MappingError:
    """One reason a row could not be mapped onto the master data."""

    error: ErrorSpec
    message: str
    field_name: str | None = None
    field_value: str | None = None


@dataclass
class MappingResult:
    """Resolved dimension keys, or the errors that prevented resolution."""

    dimensions: dict[str, Any] = field(default_factory=dict)
    errors: list[MappingError] = field(default_factory=list)
    #: True when the hierarchy came from the Customer Master rather than from
    #: codes on the row itself. Counted in the import summary so it is visible
    #: how much of a file leant on the derivation.
    derived_from_customer: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors


class MasterDataIndex:
    """In-memory snapshot of the master dimensions for one ETL run.

    Loading once and resolving in memory keeps the ETL to a handful of queries
    regardless of file size, instead of a lookup per row.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.ids: dict[str, dict[str, int]] = {}
        self.parents: dict[str, dict[str, str | None]] = {}
        self.optional: dict[str, dict[str, int]] = {}
        self.optional_status: dict[str, str] = {}
        #: ``customer_code -> sub_territory_code`` from the Customer Master.
        self.customer_sub_territory: dict[str, str] = {}
        #: ``sales_force_code -> territory_code`` from the Sales Force Master.
        self.sales_force_territory: dict[str, str] = {}
        self._load()

    # -- loading ------------------------------------------------------------

    def _load(self) -> None:
        for binding in LEVEL_BINDINGS:
            columns = [
                getattr(binding.model, binding.code_field),
                getattr(binding.model, binding.surrogate_key),
            ]
            if binding.parent_code_field:
                columns.append(getattr(binding.model, binding.parent_code_field))
            rows = self.session.execute(select(*columns)).all()

            self.ids[binding.code_field] = {r[0]: r[1] for r in rows}
            if binding.parent_code_field:
                self.parents[binding.code_field] = {r[0]: r[2] for r in rows}
            else:
                self.parents[binding.code_field] = {r[0]: None for r in rows}

        # The authoritative Customer -> Sub-territory link, used to derive a
        # row's hierarchy when the transaction file does not carry one.
        self.customer_sub_territory = {
            code: sub for code, sub in self.session.execute(
                select(DimCustomer.customer_code, DimCustomer.sub_territory_code)
            ).all() if sub
        }

        # The Sales Force Master's own record of who covers which territory,
        # used the same way: to check an assignment a transaction claims, never
        # to invent one it did not state.
        self.sales_force_territory = {
            code: territory for code, territory in self.session.execute(
                select(DimSalesForce.sales_force_code, DimSalesForce.territory_code)
            ).all() if territory
        }

        for table, model, code_col, id_col in (
            ("dim_customer", DimCustomer, DimCustomer.customer_code, DimCustomer.customer_id),
            ("dim_sales_force", DimSalesForce, DimSalesForce.sales_force_code,
             DimSalesForce.sales_force_id),
        ):
            self.optional[table] = {
                code: key for code, key in self.session.execute(select(code_col, id_col)).all()
            }

        # The three material masters, one query each. A stock file of any size
        # resolves every row against dictionaries already in memory, never with
        # a query per row. ``self.materials`` serves the sales and target
        # datasets too since revision 0022 — one master, one lookup.
        #
        # ``company|plant`` -> plant_id. Keyed on the pair because a plant code
        # identifies a plant within a company, not globally.
        self.plants = {
            key: pid for key, pid in self.session.execute(
                select(DimPlant.plant_key, DimPlant.plant_id)).all()
        }
        #: ``plant|storage location`` -> storage_location_id, for the same
        #: reason: a storage location code is unique only inside its plant.
        self.storage_locations = {
            key: sid for key, sid in self.session.execute(
                select(DimStorageLocation.storage_location_key,
                       DimStorageLocation.storage_location_id)).all()
        }
        #: ``material_code -> (material_id, group code, brand code)``. The group
        #: and brand come along because the stock file states both and the ETL
        #: cross-checks them; they are attributes of the material, so the master
        #: is the one place they are recorded.
        self.materials = {
            code: (mid, group, brand) for code, mid, group, brand in
            self.session.execute(
                select(DimMaterial.material_code, DimMaterial.material_id,
                       DimMaterial.material_group_code,
                       DimMaterial.material_brand_code)).all()
        }

        statuses = {
            row.table_name: row.status
            for row in self.session.execute(select(MasterSourceStatus)).scalars()
        }
        for table in ("dim_customer", "dim_sales_force"):
            self.optional_status[table] = statuses.get(table, STATUS_PENDING_SOURCE_DATA)

    # -- introspection ------------------------------------------------------

    @property
    def is_empty(self) -> bool:
        """True when no master data has been loaded at all."""
        return not any(self.ids.values()) and not self.materials

    def counts(self) -> dict[str, int]:
        counts = {level: len(codes) for level, codes in self.ids.items()}
        counts["material_code"] = len(self.materials)
        return counts

    def ancestors_of(self, level: str, code: str) -> dict[str, str]:
        """Every ancestor code of ``code``, keyed by level. Includes ``level``."""
        chain: dict[str, str] = {level: code}
        current_level, current_code = level, code
        while True:
            binding = BINDING_BY_LEVEL[current_level]
            if binding.parent_code_field is None:
                break
            parent_code = self.parents[current_level].get(current_code)
            if parent_code is None:
                break
            chain[binding.parent_code_field] = parent_code
            current_level, current_code = binding.parent_code_field, parent_code
        return chain

    # -- resolution ---------------------------------------------------------

    def derived_sub_territory(self, record: dict[str, Any]) -> str | None:
        """The sub-territory a row's customer belongs to, per the Customer Master.

        The hierarchy stored on a transaction is a *snapshot* of where the
        customer sat when it traded, and a source file often carries the
        customer code without repeating the eight organisational levels above
        it. Rather than make an operator type them — or, worse, guess them —
        the deepest level is read from the master and the rest is derived from
        it by the existing chain walk.

        Only ever the *recorded* link. A customer the master does not know, or
        one whose sub-territory is blank, returns ``None`` and the row is
        reported as unmapped instead.
        """
        code = record.get("customer_code")
        if _blank(code):
            return None
        return self.customer_sub_territory.get(str(code))

    def resolve_org(self, record: dict[str, Any],
                    org_levels: Iterable[str] = ORG_LEVELS) -> MappingResult:
        """Resolve and cross-check the organisational codes on one row."""
        result = MappingResult()
        levels = [lvl for lvl in org_levels if lvl in BINDING_BY_LEVEL]
        provided = {
            lvl: record.get(lvl) for lvl in levels if not _blank(record.get(lvl))
        }

        if not provided and "sub_territory_code" in levels:
            derived = self.derived_sub_territory(record)
            if derived is not None:
                provided = {"sub_territory_code": derived}
                result.derived_from_customer = True

        if not provided:
            if not _blank(record.get("customer_code")):
                result.errors.append(MappingError(
                    errors.CUSTOMER_HIERARCHY_MISSING,
                    "Customer hierarchy mapping missing: customer "
                    f"'{record.get('customer_code')}' has no sub-territory in the "
                    "Customer Master, and the row carries no organisational code "
                    "of its own.",
                    field_name="customer_code",
                    field_value=str(record.get("customer_code")),
                ))
                return result
            result.errors.append(MappingError(
                errors.NO_ORGANISATIONAL_CODE,
                "The row carries no organisational code, so it cannot be attached to the "
                "master hierarchy.",
            ))
            return result

        # 1. existence
        unknown: set[str] = set()
        for level, code in provided.items():
            if code not in self.ids[level]:
                unknown.add(level)
                result.errors.append(MappingError(
                    errors.MASTER_ERROR_BY_LEVEL[level],
                    f"{level.replace('_', ' ')} '{code}' does not exist in "
                    f"{BINDING_BY_LEVEL[level].model.__tablename__}.",
                    field_name=level, field_value=str(code),
                ))
        known = {lvl: code for lvl, code in provided.items() if lvl not in unknown}
        if not known:
            return result

        # 2. derive the full chain from the deepest known level
        deepest = max(known, key=lambda lvl: LEVEL_DEPTH[lvl])
        chain = self.ancestors_of(deepest, known[deepest])

        # 3. every other supplied level must agree with the derived chain
        for level, code in known.items():
            if level == deepest:
                continue
            derived = chain.get(level)
            if derived is None:
                result.errors.append(MappingError(
                    errors.BROKEN_MASTER_HIERARCHY,
                    f"'{known[deepest]}' has no {level.replace('_', ' ')} in the master "
                    "hierarchy, so the row's levels cannot be cross-checked.",
                    field_name=level, field_value=str(code),
                ))
                continue
            if derived != code:
                spec = errors.HIERARCHY_ERROR_BY_PAIR.get(
                    (deepest, level), errors.HIERARCHY_MISMATCH
                )
                result.errors.append(MappingError(
                    spec,
                    f"{deepest.replace('_', ' ')} '{known[deepest]}' belongs to "
                    f"{level.replace('_', ' ')} '{derived}', but the row says '{code}'.",
                    field_name=level, field_value=str(code),
                ))

        if result.errors:
            return result

        for level, code in chain.items():
            binding = BINDING_BY_LEVEL[level]
            result.dimensions[binding.dimension_field] = self.ids[level].get(code)
        return result

    def resolve_material_code(self, material_code: Any, required: bool) -> MappingResult:
        """Resolve a Material Code to ``material_id``.

        The item lookup for the datasets that name one item and nothing about
        where it is held — sales and target. A stock position names three masters
        and goes through :meth:`resolve_stock_masters` instead.

        Since revision 0022 this is the *only* item resolution in the ETL: the
        SKU dimension it replaced held a second identity for the same goods, and
        a sale that resolved against one master while a stock position resolved
        against another could not be reported together.
        """
        result = MappingResult()
        if _blank(material_code):
            if required:
                result.errors.append(MappingError(
                    errors.MISSING_REQUIRED_FIELD,
                    "Material code is required for this data type but is empty.",
                    field_name="material_code",
                ))
            else:
                result.dimensions["material_id"] = None
            return result

        known = self.materials.get(material_code)
        if known is None:
            result.errors.append(MappingError(
                errors.INVALID_MATERIAL_CODE,
                f"Material code '{material_code}' does not exist in the Material "
                "Master. Load the Material Master for this material first.",
                field_name="material_code", field_value=str(material_code),
            ))
            return result
        result.dimensions["material_id"] = known[0]
        return result

    def resolve_stock_masters(self, record: dict[str, Any]) -> MappingResult:
        """Resolve a stock row against the Plant, Storage Location and Material
        Masters, and check the attributes it repeats from the last of them.

        Unlike the pending dimensions above, an unknown code here is **always**
        a rejection. Those tolerate unknown codes because their masters may not
        have been loaded yet and the transaction still carries the code for later
        back-filling. A material stock position is different: without the masters
        it has no plant name, no storage-location name, no material description
        and no group or brand, so it could not be reported at all.

        Each master is resolved separately and reports its own failure, because
        each is corrected by a different upload. All three are checked even when
        the first fails, so a file with a missing plant *and* an unknown material
        says so once rather than over two import attempts.

        The group and brand the row states are then compared against what the
        Material Master records for that material. They are attributes of the
        material, so the two cannot legitimately disagree; which side is wrong
        this system cannot know, so it reports the disagreement and refuses
        rather than letting either silently win.
        """
        result = MappingResult()
        company = record.get("company_code")
        plant = record.get("plant_code")
        storage = record.get("storage_location_code")
        material = record.get("material_code")
        group = record.get("material_group_code")
        brand = record.get("material_brand_code")

        plant_id = self.plants.get(plant_key(company, plant))
        if plant_id is None:
            result.errors.append(MappingError(
                errors.INVALID_PLANT_CODE,
                f"No Plant Master record for company '{company}', plant "
                f"'{plant}'. Load the Plant Master for this plant first.",
                field_name="plant_code", field_value=str(plant),
            ))
        else:
            result.dimensions["plant_id"] = plant_id

        storage_id = self.storage_locations.get(storage_location_key(plant, storage))
        if storage_id is None:
            result.errors.append(MappingError(
                errors.INVALID_STORAGE_LOCATION_CODE,
                f"No Storage Location Master record for plant '{plant}', "
                f"storage location '{storage}'. Load the Storage Location "
                "Master for this storage location first.",
                field_name="storage_location_code", field_value=str(storage),
            ))
        else:
            result.dimensions["storage_location_id"] = storage_id

        known = self.materials.get(material)
        if known is None:
            result.errors.append(MappingError(
                errors.INVALID_MATERIAL_CODE,
                f"Material code '{material}' does not exist in the Material "
                "Master. Load the Material Master for this material first.",
                field_name="material_code", field_value=str(material),
            ))
            return result

        material_id, known_group, known_brand = known
        result.dimensions["material_id"] = material_id
        if known_group != group:
            result.errors.append(MappingError(
                errors.MATERIAL_GROUP_MISMATCH,
                f"Material '{material}' is registered in the Material Master "
                f"under material group '{known_group}', but this row states "
                f"'{group}'. One of the two is wrong; correct the file or the "
                "Material Master.",
                field_name="material_group_code", field_value=str(group),
            ))
        if known_brand != brand:
            result.errors.append(MappingError(
                errors.MATERIAL_BRAND_MISMATCH,
                f"Material '{material}' is registered in the Material Master "
                f"under material brand '{known_brand}', but this row states "
                f"'{brand}'. One of the two is wrong; correct the file or the "
                "Material Master.",
                field_name="material_brand_code", field_value=str(brand),
            ))
        return result

    def resolve_assignment(self, record: dict[str, Any]) -> MappingResult:
        """Cross-check the row's customer and sales force against their masters.

        Both masters record where their record *belongs*: a customer has a
        sub-territory, a sales force member has a territory. A row that names
        one of them alongside a different territory is claiming an assignment
        the master contradicts, and one of the two is wrong. Which one this
        system cannot know, so it reports the disagreement and rejects the row
        rather than picking a side.

        Silent in three cases, each deliberate. A code the row does not carry is
        nothing to check. A code the master does not know is already handled by
        :meth:`resolve_optional`, which tolerates it while the dimension is
        ``PENDING_SOURCE_DATA`` and rejects it once a real master exists. And a
        master record with no territory recorded on it is a gap in the master,
        not a defect in the transaction — filling it in from the row would be
        inventing the very mapping this check exists to verify.
        """
        result = MappingResult()
        territory = record.get("territory_code")
        sub_territory = record.get("sub_territory_code")

        customer = record.get("customer_code")
        if not _blank(customer):
            recorded = self.customer_sub_territory.get(str(customer))
            if recorded is not None:
                if not _blank(sub_territory):
                    if recorded != sub_territory:
                        result.errors.append(MappingError(
                            errors.CUSTOMER_TERRITORY_MISMATCH,
                            f"Customer '{customer}' belongs to sub-territory "
                            f"'{recorded}' in the Customer Master, but the row "
                            f"says '{sub_territory}'.",
                            field_name="customer_code", field_value=str(customer),
                        ))
                elif not _blank(territory):
                    # The row named a territory but no sub-territory, so the
                    # comparison is made one level up, where both are stated.
                    owner = self.ancestors_of(
                        "sub_territory_code", recorded
                    ).get("territory_code")
                    if owner is not None and owner != territory:
                        result.errors.append(MappingError(
                            errors.CUSTOMER_TERRITORY_MISMATCH,
                            f"Customer '{customer}' belongs to territory "
                            f"'{owner}' through sub-territory '{recorded}', but "
                            f"the row says '{territory}'.",
                            field_name="customer_code", field_value=str(customer),
                        ))

        sales_force = record.get("sales_force_code")
        if not _blank(sales_force) and not _blank(territory):
            recorded = self.sales_force_territory.get(str(sales_force))
            if recorded is not None and recorded != territory:
                result.errors.append(MappingError(
                    errors.SALES_FORCE_TERRITORY_MISMATCH,
                    f"Sales force '{sales_force}' is assigned to territory "
                    f"'{recorded}' in the Sales Force Master, but the row says "
                    f"'{territory}'.",
                    field_name="sales_force_code", field_value=str(sales_force),
                ))

        return result

    def resolve_optional(self, table: str, dimension_field: str, code: Any) -> MappingResult:
        """Resolve customer / sales-force codes.

        While the dimension is ``PENDING_SOURCE_DATA`` an unknown code is *not*
        an error: the key is left NULL and the code is kept on the fact row for
        later back-filling. Once a real master is loaded and the status flips to
        ``AVAILABLE``, the same code becomes a rejection.
        """
        result = MappingResult()
        result.dimensions[dimension_field] = None
        if _blank(code):
            return result

        key = self.optional[table].get(code)
        if key is not None:
            result.dimensions[dimension_field] = key
            return result

        if self.optional_status.get(table) == STATUS_AVAILABLE:
            result.errors.append(MappingError(
                errors.OPTIONAL_DIMENSION_ERRORS[table],
                f"Code '{code}' does not exist in {table}.",
                field_name=dimension_field.replace("_id", "_code"), field_value=str(code),
            ))
        return result


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def ensure_master_source_status(session: Session) -> None:
    """Seed / refresh ``etl_master_source_status``.

    Phase 1 dimensions are ``AVAILABLE`` (their source sheet exists in
    ``Master Data.xlsx``, whether or not it currently holds records). The three
    future-ready dimensions stay ``PENDING_SOURCE_DATA`` until a real master file
    is imported for them.
    """
    from ..master_data.schema import TABLE_SPECS

    rows = {
        spec.table: (
            STATUS_AVAILABLE,
            f"Sheet '{spec.sheet}' in Master Data.xlsx",
            "Phase 1 master dimension.",
        )
        for spec in TABLE_SPECS
    }
    rows.update({
        "dim_customer": (STATUS_PENDING_SOURCE_DATA, None,
                         "No Customer Master sheet exists yet. Structure is ready; no "
                         "customer record is invented."),
        "dim_sales_force": (STATUS_PENDING_SOURCE_DATA, None,
                            "No Sales Force Master sheet exists yet."),
    })

    existing = {
        row.table_name: row
        for row in session.execute(select(MasterSourceStatus)).scalars()
    }
    for table, (status, source, note) in rows.items():
        current = existing.get(table)
        if current is None:
            session.add(MasterSourceStatus(
                table_name=table, status=status, source_description=source, note=note
            ))
        elif current.status not in (STATUS_AVAILABLE, STATUS_PENDING_SOURCE_DATA):
            current.status = status


__all__ = [
    "LevelBinding",
    "LEVEL_BINDINGS",
    "BINDING_BY_LEVEL",
    "LEVEL_DEPTH",
    "MappingError",
    "MappingResult",
    "MasterDataIndex",
    "ensure_master_source_status",
]
