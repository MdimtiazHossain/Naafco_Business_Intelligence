"""Load published administrative reference points from a GeoJSON file.

Companion to ``import_admin_areas.py``. The boundary datasets ship point layers
alongside the polygons — administrative capitals, and label positions for the
units themselves — and this loads them into ``map_admin_points``.

**These are not business locations.** ``map_entity_locations`` records where
somebody placed a customer, a warehouse or a territory, and carries a source and
a precision describing that decision. These points arrive with a published file
and are replaced by the next one. Keeping them apart is what lets "who put this
here?" stay answerable for both.

Two kinds, named by ``--kind``:

* ``capital`` — the seat of an administrative unit (``bgd_admincapitals``).
* ``point`` — a label position for the unit as a whole (``bgd_adminpoints``).

The level of each point is read from the file, never inferred. The HDX release
publishes it as ``adm_p_lvl`` on the capitals layer and ``admin_level`` on the
points layer; both are recognised, along with the ancestor P-codes, which are
stored for filtering without a foreign key — the point files reach a level below
anything this schema names, and a constraint would reject real places.

A re-import replaces the layer it names and leaves the other alone, because a
published point has no stable identifier to match on: the files carry no point
id, so matching on position would strand a point the publisher moved. Replacing
one kind wholesale is honest about what the source actually supports.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.database.connection import get_engine  # noqa: E402
from app.database.models_geo import AdminPointKind, MapAdminPoint  # noqa: E402

#: Where each field is published, in the order tried.
ALIASES: dict[str, tuple[str, ...]] = {
    "level": ("adm_p_lvl", "admin_level", "ADM_P_LVL", "ADMIN_LEVEL"),
    "name": ("name", "NAME", "adm_name"),
    "name_bn": ("name_bn", "NAME_BN"),
    "country_code": ("adm0_pcode", "ADM0_PCODE"),
    "division_code": ("adm1_pcode", "ADM1_PCODE"),
    "district_code": ("adm2_pcode", "ADM2_PCODE"),
    "upazila_code": ("adm3_pcode", "ADM3_PCODE"),
}


@dataclass
class Counts:
    features: int = 0
    loaded: int = 0
    removed: int = 0
    skipped: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.skipped is None:
            self.skipped = []


def pick(properties: dict[str, Any], key: str) -> Any:
    for alias in ALIASES.get(key, ()):
        value = properties.get(alias)
        if value not in (None, ""):
            return value
    return None


def position(feature: dict[str, Any]) -> tuple[float, float] | None:
    """Longitude/latitude of a Point feature, or None if it is not one.

    Read from the geometry rather than from the ``x_coord`` / ``y_coord``
    properties the files also carry: the geometry is what every other consumer
    draws, and if the two ever disagreed, drawing one while storing the other
    would put the marker somewhere nothing else agrees it is.
    """
    geometry = feature.get("geometry") or {}
    if geometry.get("type") != "Point":
        return None
    coordinates = geometry.get("coordinates")
    if (not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2):
        return None
    try:
        longitude, latitude = float(coordinates[0]), float(coordinates[1])
    except (TypeError, ValueError):
        return None
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        return None
    return longitude, latitude


def load(path: Path, *, kind: str, actor: str, dry_run: bool) -> Counts:
    counts = Counts()
    document = json.loads(path.read_text(encoding="utf-8"))

    crs = document.get("crs") or {}
    name = str((crs.get("properties") or {}).get("name", "")).upper()
    if name and "CRS84" not in name and "4326" not in name:
        raise SystemExit(
            f"{path.name} declares coordinate system '{name}', which is not "
            "WGS84 longitude/latitude. Reproject it to EPSG:4326 first."
        )

    features = document.get("features") or []
    if not features:
        raise SystemExit(f"{path.name} contains no features.")

    rows: list[dict[str, Any]] = []
    seen: set[tuple[int, float, float]] = set()
    for index, feature in enumerate(features):
        counts.features += 1
        properties = feature.get("properties") or {}

        where = position(feature)
        if where is None:
            counts.skipped.append(f"feature {index}: not a usable Point geometry")
            continue
        longitude, latitude = where

        label = pick(properties, "name")
        if not label:
            counts.skipped.append(f"feature {index}: no name property")
            continue

        raw_level = pick(properties, "level")
        try:
            level = int(raw_level)
        except (TypeError, ValueError):
            counts.skipped.append(
                f"{label}: administrative level is '{raw_level}', not a number")
            continue

        # The unique constraint is (kind, level, lat, lon); a file repeating one
        # would fail the insert, so it is reported rather than hitting the
        # database as an integrity error.
        key = (level, round(latitude, 7), round(longitude, 7))
        if key in seen:
            counts.skipped.append(f"{label}: duplicate of an earlier point")
            continue
        seen.add(key)

        rows.append({
            "kind": kind,
            "admin_level": level,
            "name": str(label).strip(),
            "name_bn": pick(properties, "name_bn"),
            "latitude": latitude,
            "longitude": longitude,
            "country_code": pick(properties, "country_code"),
            "division_code": pick(properties, "division_code"),
            "district_code": pick(properties, "district_code"),
            "upazila_code": pick(properties, "upazila_code"),
            "source_file": path.name,
            "imported_by": actor,
        })

    counts.loaded = len(rows)
    if dry_run:
        return counts

    with Session(get_engine()) as session:
        counts.removed = len(list(session.execute(
            select(MapAdminPoint.point_id)
            .where(MapAdminPoint.kind == kind)).scalars()))
        session.execute(delete(MapAdminPoint).where(MapAdminPoint.kind == kind))
        session.bulk_insert_mappings(MapAdminPoint, rows)
        session.commit()

    return counts


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", type=Path, help="GeoJSON file of point features.")
    parser.add_argument("--kind", choices=list(AdminPointKind.ALL), required=True,
                        help="Which published layer this file is.")
    parser.add_argument("--actor", default="import_admin_points",
                        help="Recorded as the importer.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and report without writing.")
    args = parser.parse_args()

    if not args.path.exists():
        print(f"No such file: {args.path}")
        return 2

    print(f"Reading {args.path.name} as '{args.kind}'")
    if args.dry_run:
        print("DRY RUN — nothing will be written")

    counts = load(args.path, kind=args.kind, actor=args.actor,
                  dry_run=args.dry_run)

    print(f"\nFeatures read:  {counts.features:,}")
    print(f"Points loaded:  {counts.loaded:,}")
    if counts.removed:
        print(f"Replaced:       {counts.removed:,} existing '{args.kind}' points")
    if counts.skipped:
        print(f"\nSkipped {len(counts.skipped)}:")
        for line in counts.skipped[:20]:
            print(f"  {line}")
        if len(counts.skipped) > 20:
            print(f"  … and {len(counts.skipped) - 20} more")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
