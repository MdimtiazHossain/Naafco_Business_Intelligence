"""Load Bangladesh administrative boundaries from a GeoJSON file.

The boundaries themselves are **not** bundled with this project. Polygon
geometry cannot be invented — a fabricated outline renders as convincingly as a
real one and is wrong in a way nobody notices — so this loads a published file
instead. Any of these work, all of them free:

* **HDX / OCHA COD** — https://data.humdata.org/dataset/cod-ab-bgd. Ships one
  file per level (``bgd_admin0`` … ``bgd_admin3``) with official P-codes.
  Properties arrive as ``adm1_name`` / ``adm1_pcode`` in the current release and
  as ``ADM1_EN`` / ``ADM1_PCODE`` in older ones; both are recognised.
* **GADM** — https://gadm.org/download_country.html (Bangladesh, level 3).
  Properties arrive as ``NAME_1`` / ``NAME_2`` / ``NAME_3``.
* **Natural Earth**, or any local authority export.

Property names are auto-detected from the aliases below, so a file from any of
these sources loads without being reshaped first. When a file uses names none of
them cover, ``--property-map`` names them explicitly.

**The level is detected, not assumed.** A file is read for the deepest level it
names — a file carrying ``adm2_*`` and nothing below it is a district file — and
the boundary is stored at that level. A single file naming every level down to
the upazila therefore behaves exactly as it did before this was generalised.
Pass ``--level`` to override the detection.

**Ancestors come from the same feature.** Every published feature repeats its
parents' names and codes, so loading a district file also creates the country
and division rows it references. Loading all four files in order gives four
levels of dimension rows and four levels of polygon, with no name matching
anywhere: the P-codes are nested (``BD`` → ``BD10`` → ``BD1004``), so the
hierarchy is carried by the codes themselves.

What the import does per feature:

1. reads each level's name and code, deepest first;
2. creates the dimension rows from the country down, parents first, upserting by
   code so re-running is safe;
3. validates the geometry, simplifies it, rounds it and stores it against the
   detected level with its bounding box and centroid.

Nothing is deleted. A re-import updates what it recognises and leaves the rest,
so loading a corrected file for one district does not disturb the others.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database.connection import get_engine  # noqa: E402
from app.database.models_geo import (  # noqa: E402
    BoundarySource,
    DimCountry,
    DimDistrict,
    DimDivision,
    DimUpazila,
    MapAreaBoundary,
)
from app.map import geometry as geo  # noqa: E402


@dataclass(frozen=True)
class Level:
    """One administrative level, and how to read and store it."""

    key: str
    depth: int
    model: Any
    code_field: str
    name_field: str
    bn_field: str
    parent_field: str | None


#: Shallowest first. ``depth`` matches the map's own level registry.
LEVELS: tuple[Level, ...] = (
    Level("country", 0, DimCountry, "country_code", "country_name",
          "country_name_bn", None),
    Level("division", 1, DimDivision, "division_code", "division_name",
          "division_name_bn", "country_code"),
    Level("district", 2, DimDistrict, "district_code", "district_name",
          "district_name_bn", "division_code"),
    Level("upazila", 3, DimUpazila, "upazila_code", "upazila_name",
          "upazila_name_bn", "district_code"),
)
LEVEL_BY_KEY = {level.key: level for level in LEVELS}

#: Property names each field is published under, in the order they are tried.
#: Longest-standing conventions first so a file carrying several agrees with
#: what a human would expect. The ``adm*_name`` forms are the current HDX COD
#: release; ``ADM*_EN`` is the older one; ``NAME_*`` is GADM.
ALIASES: dict[str, tuple[str, ...]] = {
    "country": ("ADM0_EN", "adm0_name", "NAME_0", "country", "Country",
                "adm0_en"),
    "division": ("ADM1_EN", "adm1_name", "NAME_1", "division", "Division",
                 "DIVISION", "div_name", "adm1_en"),
    "district": ("ADM2_EN", "adm2_name", "NAME_2", "district", "District",
                 "DISTRICT", "dis_name", "zila", "adm2_en"),
    "upazila": ("ADM3_EN", "adm3_name", "NAME_3", "upazila", "Upazila",
                "UPAZILA", "upa_name", "thana", "sub_district", "adm3_en"),
    "country_code": ("ADM0_PCODE", "adm0_pcode", "country_code", "iso3"),
    "division_code": ("ADM1_PCODE", "adm1_pcode", "division_code", "div_code"),
    "district_code": ("ADM2_PCODE", "adm2_pcode", "district_code", "dis_code"),
    "upazila_code": ("ADM3_PCODE", "adm3_pcode", "upazila_code", "upa_code"),
    "country_bn": ("ADM0_BN", "country_bn"),
    "division_bn": ("ADM1_BN", "division_bn"),
    "district_bn": ("ADM2_BN", "district_bn"),
    "upazila_bn": ("ADM3_BN", "upazila_bn"),
    # The published administrative centre, where the source states one. Used in
    # preference to a computed centroid: it is what the publisher intends the
    # label to sit on, and a computed centroid of a coastal district can fall
    # in the sea.
    "center_lat": ("center_lat", "CENTER_LAT", "centre_lat", "y_coord"),
    "center_lon": ("center_lon", "CENTER_LON", "centre_lon", "x_coord"),
    "iso2": ("iso2", "ISO2"),
    "iso3": ("iso3", "ISO3"),
}

#: Prefixes for codes derived from a name, so a derived code is recognisable as
#: one and can never collide with an official P-code.
CODE_PREFIX = {"country": "CTY", "division": "DIV", "district": "DIS",
               "upazila": "UPZ"}


@dataclass
class Counts:
    features: int = 0
    created: dict[str, int] = field(default_factory=dict)
    boundaries: int = 0
    updated: int = 0
    skipped: list[str] = field(default_factory=list)
    source_points: int = 0
    stored_points: int = 0

    def created_one(self, key: str) -> None:
        self.created[key] = self.created.get(key, 0) + 1


def pick(properties: dict[str, Any], key: str,
         overrides: dict[str, str]) -> str | None:
    """The value for one logical field, by override then by alias."""
    explicit = overrides.get(key)
    if explicit:
        value = properties.get(explicit)
        return str(value).strip() if value not in (None, "") else None
    for alias in ALIASES.get(key, ()):
        value = properties.get(alias)
        if value not in (None, ""):
            return str(value).strip()
    return None


def pick_float(properties: dict[str, Any], key: str,
               overrides: dict[str, str]) -> float | None:
    raw = pick(properties, key, overrides)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def derive_code(level: str, name: str, parent_code: str | None = None) -> str:
    """A stable code for a level whose file carries none.

    Deterministic, so two runs of the same file produce the same codes and the
    second is an update rather than a duplicate. The parent is folded in below
    the division because names repeat across parents — there is a Sadar in
    almost every district.
    """
    slug = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9]+", "", slug).upper()[:16] or "UNKNOWN"
    prefix = CODE_PREFIX[level]
    if parent_code:
        tail = re.sub(r"[^A-Za-z0-9]+", "", parent_code).upper()[-6:]
        return f"{prefix}-{tail}-{slug}"[:64]
    return f"{prefix}-{slug}"[:64]


def features_of(document: Any) -> Iterable[dict[str, Any]]:
    """Accept a FeatureCollection, a bare list, or a single Feature."""
    if isinstance(document, dict):
        if document.get("type") == "FeatureCollection":
            return document.get("features") or []
        if document.get("type") == "Feature":
            return [document]
    if isinstance(document, list):
        return document
    raise SystemExit(
        "The file is not GeoJSON. Expected a FeatureCollection with a "
        "'features' array."
    )


def detect_level(features: list[dict[str, Any]],
                 overrides: dict[str, str]) -> Level:
    """The deepest level the file actually names.

    Read from a sample rather than the first feature alone: a single feature
    with a null name would otherwise decide the level for the whole file.
    """
    sample = features[: min(len(features), 50)]
    deepest = LEVELS[0]
    for level in LEVELS:
        if any(pick(f.get("properties") or {}, level.key, overrides)
               for f in sample):
            deepest = level
    return deepest


def check_crs(document: dict[str, Any], path: Path) -> None:
    """Refuse a file that declares a projection this cannot read.

    RFC 7946 GeoJSON is WGS84 longitude/latitude and says so by omitting ``crs``;
    older files name CRS84 or EPSG:4326, which are the same thing. Anything else
    is projected, and silently treating projected metres as degrees would place
    the whole country off the coast of Africa — so it stops instead.
    """
    crs = document.get("crs")
    if not crs:
        return
    name = str((crs.get("properties") or {}).get("name", "")).upper()
    if not name:
        return
    if "CRS84" in name or "4326" in name:
        return
    raise SystemExit(
        f"{path.name} declares coordinate system '{name}', which is not WGS84 "
        "longitude/latitude. Reproject it to EPSG:4326 before importing — this "
        "script will not guess a transformation."
    )


def upsert(session: Session, model: Any, code_field: str, code: str,
           values: dict[str, Any]) -> bool:
    """Create or update one dimension row. Returns True when created."""
    existing = session.execute(
        select(model).where(getattr(model, code_field) == code)
    ).scalar_one_or_none()
    if existing is None:
        session.add(model(**{code_field: code, **values}))
        return True
    for key, value in values.items():
        if value is not None and getattr(existing, key) != value:
            setattr(existing, key, value)
    # A re-import of a retired area brings it back, the same rule the master
    # loader follows: reporting success while the record stays hidden is a trap.
    if getattr(existing, "is_deleted", False):
        existing.is_deleted = False
        existing.deleted_at = None
        existing.deleted_by = None
    return False


def load(path: Path, *, tolerance: float, overrides: dict[str, str],
         actor: str, dry_run: bool, limit: int | None,
         forced_level: str | None) -> tuple[Counts, Level]:
    counts = Counts()
    document = json.loads(path.read_text(encoding="utf-8"))
    check_crs(document, path)

    features = list(features_of(document))
    if not features:
        raise SystemExit(f"{path.name} contains no features.")

    target = (LEVEL_BY_KEY[forced_level] if forced_level
              else detect_level(features, overrides))
    ladder = [level for level in LEVELS if level.depth <= target.depth]

    engine = get_engine()

    with Session(engine) as session:
        for index, feature in enumerate(features):
            if limit is not None and counts.features >= limit:
                break
            counts.features += 1
            properties = feature.get("properties") or {}
            shape = feature.get("geometry")

            names = {level.key: pick(properties, level.key, overrides)
                     for level in ladder}
            missing = [level.key for level in ladder if not names[level.key]]
            if missing:
                counts.skipped.append(
                    f"feature {index}: no {', '.join(missing)} property "
                    f"(has: {', '.join(sorted(properties)[:8])})"
                )
                continue

            try:
                geo.validate(shape)
            except geo.InvalidGeometry as exc:
                counts.skipped.append(f"{names[target.key]}: {exc}")
                continue

            # Codes, shallowest first, each falling back to a derived one that
            # folds in its parent so it stays unique.
            codes: dict[str, str] = {}
            parent_code: str | None = None
            for level in ladder:
                codes[level.key] = (
                    pick(properties, f"{level.key}_code", overrides)
                    or derive_code(level.key, names[level.key], parent_code)
                )
                parent_code = codes[level.key]

            source_points = geo.count_points(shape)
            simplified = geo.round_coordinates(geo.simplify(shape, tolerance))
            box = geo.bounds(simplified)
            computed_lat, computed_lon = geo.centroid(simplified)
            # The publisher's own centre wins where it exists; see ALIASES.
            latitude = pick_float(properties, "center_lat", overrides)
            longitude = pick_float(properties, "center_lon", overrides)
            if latitude is None or longitude is None:
                latitude, longitude = computed_lat, computed_lon
            stored_points = geo.count_points(simplified)

            counts.source_points += source_points
            counts.stored_points += stored_points

            if dry_run:
                counts.boundaries += 1
                continue

            # Dimension rows, parents first, so a foreign key never dangles.
            for level in ladder:
                values: dict[str, Any] = {
                    level.name_field: names[level.key],
                    level.bn_field: pick(properties, f"{level.key}_bn",
                                         overrides),
                }
                if level.parent_field:
                    values[level.parent_field] = codes[
                        LEVELS[level.depth - 1].key]
                if level.key == "country":
                    values["iso2"] = pick(properties, "iso2", overrides)
                    values["iso3"] = pick(properties, "iso3", overrides)
                if level.key == "upazila":
                    values["latitude"] = round(latitude, 6)
                    values["longitude"] = round(longitude, 6)
                if upsert(session, level.model, level.code_field,
                          codes[level.key], values):
                    counts.created_one(level.key)
                session.flush()

            code = codes[target.key]
            boundary = session.execute(
                select(MapAreaBoundary).where(
                    MapAreaBoundary.entity_type == target.key,
                    MapAreaBoundary.entity_code == code,
                )
            ).scalar_one_or_none()
            values = {
                "geometry": simplified,
                "bbox_north": box.north, "bbox_south": box.south,
                "bbox_east": box.east, "bbox_west": box.west,
                "centroid_latitude": round(latitude, 6),
                "centroid_longitude": round(longitude, 6),
                "point_count": stored_points,
                "source_point_count": source_points,
                "simplify_tolerance": tolerance or None,
                "source": BoundarySource.IMPORT,
                "source_file": path.name,
                "imported_by": actor,
            }
            if boundary is None:
                session.add(MapAreaBoundary(entity_type=target.key,
                                            entity_code=code, **values))
                counts.boundaries += 1
            else:
                for key, value in values.items():
                    setattr(boundary, key, value)
                counts.updated += 1

            if counts.features % 100 == 0:
                session.flush()

        if not dry_run:
            session.commit()

    return counts, target


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", type=Path, help="GeoJSON file of admin polygons.")
    parser.add_argument("--level", choices=[level.key for level in LEVELS],
                        default=None,
                        help="Store boundaries at this level. Detected from the "
                             "file's own properties when omitted.")
    parser.add_argument("--tolerance", type=float, default=None,
                        help="Douglas-Peucker tolerance in degrees. Defaults to "
                             "MAP_AREA_SIMPLIFY_TOLERANCE. 0 stores verbatim.")
    parser.add_argument("--property-map", default="",
                        help="Explicit property names, e.g. "
                             "'division=DIV_NAME,district=DIS_NAME'.")
    parser.add_argument("--actor", default="import_admin_areas",
                        help="Recorded as the importer.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Load only the first N features. For a trial run.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and report without writing.")
    args = parser.parse_args()

    if not args.path.exists():
        print(f"No such file: {args.path}")
        return 2

    tolerance = (args.tolerance if args.tolerance is not None
                 else get_settings().map_area_simplify_tolerance)
    overrides = dict(
        pair.split("=", 1) for pair in args.property_map.split(",") if "=" in pair
    )

    print(f"Reading {args.path.name}")
    print(f"Simplify tolerance: {tolerance}°"
          f"{' (verbatim)' if not tolerance else ''}")
    if args.dry_run:
        print("DRY RUN — nothing will be written")

    counts, level = load(args.path, tolerance=tolerance, overrides=overrides,
                         actor=args.actor, dry_run=args.dry_run,
                         limit=args.limit, forced_level=args.level)

    how = "forced" if args.level else "detected"
    print(f"Level:             {level.key} ({how})")
    print(f"Features read:     {counts.features:,}")
    if not args.dry_run:
        for entry in LEVELS:
            if entry.key in counts.created:
                print(f"{entry.key.title() + ' created:':<19}"
                      f"{counts.created[entry.key]:,}")
    print(f"Boundaries added:  {counts.boundaries:,}")
    if counts.updated:
        print(f"Boundaries updated:{counts.updated:,}")
    if counts.source_points:
        kept = counts.stored_points / counts.source_points * 100
        print(f"Vertices:          {counts.source_points:,} → "
              f"{counts.stored_points:,} ({kept:.1f}% kept)")
    if counts.skipped:
        print(f"\nSkipped {len(counts.skipped)}:")
        for line in counts.skipped[:20]:
            print(f"  {line}")
        if len(counts.skipped) > 20:
            print(f"  … and {len(counts.skipped) - 20} more")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
