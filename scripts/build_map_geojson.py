"""Turn the published Bangladesh boundary release into browser-sized map assets.

The map draws its administrative layers from files served with the frontend, not
from an API. That is the whole point of the MapLibre rebuild: boundaries change
only when somebody imports a new release, so re-asking a server for them on every
page load is work with no answer that ever differs.

What stops that being simple is size. The HDX COD-AB v03 release this project
carries is **118 MB** across seven files — ``bgd_admin3`` alone is 48 MB, and
``bgd_adminlines`` 30 MB. Those are survey-grade files: an upazila outline holds
several thousand vertices, of which a few hundred are distinguishable at any zoom
a dashboard uses. Shipping them raw would mean a browser parsing tens of
megabytes of JSON before the first polygon appears.

So this script reduces them, and reduces them in exactly two ways, both of which
only ever *discard precision*:

1. **Douglas–Peucker**, at a tolerance chosen per level — coarser for the country
   outline, which is only ever seen whole, finer for upazilas, which are zoomed
   into. The implementation is ``app.map.geometry.simplify``, the same one the
   database import uses, so a boundary looks the same whichever path it took.
2. **Coordinate rounding**, to five decimal places — about a metre.

Nothing is invented, no feature is dropped for being awkward, and no geometry is
repaired. A property is either copied from the source or absent; the trimmed
property sets below name every field that survives, because a browser has no use
for the fourteen ``*_name1``/``lang2``/``valid_to`` columns the release carries
and they cost more than the geometry does at this size.

One file is *derived* rather than trimmed: ``bgd_mask.geojson``, a world-sized
rectangle with Bangladesh's outer rings punched out as holes. It exists so the
map can dim everything that is not Bangladesh with a single fill layer, and it is
built from ``bgd_admin0`` here rather than in the browser so that no client has to
do polygon arithmetic on load.

Run it whenever the boundary release is replaced::

    python scripts/build_map_geojson.py            # writes frontend/public/geo/
    python scripts/build_map_geojson.py --check    # report only, write nothing

The output is committed with the frontend, so a fresh checkout has a working map
without running anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import _bootstrap  # noqa: F401  (puts backend/ on sys.path, forces UTF-8 stdout)

from app.map import geometry as geo

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "bgd_admin_boundaries.geojson"
OUTPUT_DIR = ROOT / "frontend" / "public" / "geo"

#: Metre-ish. Finer than any screen this renders on, and it roughly halves the
#: payload because the published files carry twelve decimal places or more.
PLACES = 5

#: The rectangle the mask covers.
#:
#: Web Mercator cannot draw the poles, and MapLibre clamps latitude to about
#: ±85.05° anyway, so a taller rectangle would be silently cropped. The extra
#: degree of headroom keeps the mask's edge outside the viewport at every zoom.
WORLD_RING = [
    [-180.0, -86.0], [180.0, -86.0], [180.0, 86.0], [-180.0, 86.0], [-180.0, -86.0],
]


def first(properties: dict[str, Any], *names: str) -> Any:
    """The first of ``names`` the source actually carries.

    The release renames its columns between versions (``ADM1_PCODE`` in one,
    ``adm1_pcode`` in the next), and a missing code must surface as ``None`` for
    the caller to reject rather than as a silently invented one.
    """
    for name in names:
        if name in properties and properties[name] not in (None, ""):
            return properties[name]
    return None


@dataclass(frozen=True)
class Layer:
    """One source file and what it becomes."""

    source: str
    output: str
    #: Douglas–Peucker tolerance in degrees. Zero leaves the geometry alone.
    tolerance: float
    #: Source properties → the handful the browser reads.
    properties: Callable[[dict[str, Any]], dict[str, Any]]
    #: The property whose value identifies a feature, for MapLibre `promoteId`.
    id_property: str | None = "code"
    description: str = ""
    #: Features for which this returns ``False`` are not written. Only ever used
    #: to drop features the map has no layer for, never to fix data.
    keep: Callable[[dict[str, Any]], bool] = field(default=lambda _: True)


LAYERS: tuple[Layer, ...] = (
    Layer(
        source="bgd_admin0.geojson",
        output="bgd_admin0.geojson",
        # The country outline is drawn at country zoom and as the mask's hole;
        # neither use can resolve more than this, and it is the file every page
        # load needs, so it gets the most aggressive tolerance of the four.
        tolerance=0.004,
        description="Bangladesh national outline",
        properties=lambda p: {
            "code": first(p, "adm0_pcode", "ADM0_PCODE"),
            "name": first(p, "adm0_name", "ADM0_EN"),
        },
    ),
    Layer(
        source="bgd_admin1.geojson",
        output="bgd_admin1.geojson",
        tolerance=0.003,
        description="Divisions",
        properties=lambda p: {
            "code": first(p, "adm1_pcode", "ADM1_PCODE"),
            "name": first(p, "adm1_name", "ADM1_EN"),
            "parent_code": first(p, "adm0_pcode", "ADM0_PCODE"),
        },
    ),
    Layer(
        source="bgd_admin2.geojson",
        output="bgd_admin2.geojson",
        tolerance=0.002,
        description="Districts",
        properties=lambda p: {
            "code": first(p, "adm2_pcode", "ADM2_PCODE"),
            "name": first(p, "adm2_name", "ADM2_EN"),
            "parent_code": first(p, "adm1_pcode", "ADM1_PCODE"),
            "division_name": first(p, "adm1_name", "ADM1_EN"),
        },
    ),
    Layer(
        source="bgd_admin3.geojson",
        output="bgd_admin3.geojson",
        # The deepest level anyone zooms to, so the finest tolerance — and the
        # largest file even after reduction. It is loaded on demand rather than
        # at startup for exactly that reason.
        tolerance=0.001,
        description="Upazilas",
        properties=lambda p: {
            "code": first(p, "adm3_pcode", "ADM3_PCODE"),
            "name": first(p, "adm3_name", "ADM3_EN"),
            "parent_code": first(p, "adm2_pcode", "ADM2_PCODE"),
            "district_name": first(p, "adm2_name", "ADM2_EN"),
            "division_name": first(p, "adm1_name", "ADM1_EN"),
        },
    ),
    Layer(
        source="bgd_admincapitals.geojson",
        output="bgd_admincapitals.geojson",
        tolerance=0.0,  # A point has nothing to simplify.
        description="Administrative capitals",
        id_property=None,
        properties=lambda p: {
            "name": first(p, "name", "NAME"),
            # 0 country, 1 division, 2 district, 3 upazila. Read from the file,
            # never inferred: the layer draws a different label size per level.
            "level": first(p, "adm_p_lvl", "admin_level"),
            "district_name": first(p, "adm2_name"),
            "division_name": first(p, "adm1_name"),
        },
    ),
    Layer(
        source="bgd_adminpoints.geojson",
        output="bgd_adminpoints.geojson",
        tolerance=0.0,
        description="Administrative label points",
        id_property=None,
        # Level 4 is below anything this schema names — 5,160 of the 5,777
        # points — and there is no layer that could draw them. Dropping them is
        # dropping features the map has no level for, not filtering data.
        keep=lambda p: isinstance(first(p, "admin_level", "adm_p_lvl"), int)
        and int(first(p, "admin_level", "adm_p_lvl")) <= 3,
        properties=lambda p: {
            "name": first(p, "name", "NAME"),
            "level": first(p, "admin_level", "adm_p_lvl"),
        },
    ),
    Layer(
        source="bgd_adminlines.geojson",
        output="bgd_adminlines.geojson",
        tolerance=0.002,
        description="Administrative boundary lines",
        id_property=None,
        properties=lambda p: {
            # The release uses 0–3 for administrative levels and 99/991 for the
            # coastline and disputed segments. Passed through as it arrives so
            # the style can decide; renaming it here would be a guess.
            "level": first(p, "adm_level", "admin_level"),
        },
    ),
)


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(
            f"Missing boundary file: {path}\n"
            "The published release is not bundled with this repository. Download "
            "the Bangladesh COD-AB set from HDX and unpack it into "
            f"{SOURCE_DIR}."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def reduce_feature(feature: dict[str, Any], layer: Layer) -> dict[str, Any] | None:
    """Trim one feature's properties and reduce its geometry.

    Returns ``None`` for a feature the layer excludes or one with no geometry —
    the caller counts those rather than substituting anything for them.
    """
    source_properties = feature.get("properties") or {}
    if not layer.keep(source_properties):
        return None

    shape = feature.get("geometry")
    if not shape or not shape.get("coordinates"):
        return None

    if layer.tolerance > 0:
        shape = geo.simplify(shape, layer.tolerance)
    shape = geo.round_coordinates(shape, PLACES)

    properties = {
        key: value
        for key, value in layer.properties(source_properties).items()
        if value is not None
    }

    reduced: dict[str, Any] = {
        "type": "Feature",
        "properties": properties,
        "geometry": shape,
    }
    if layer.id_property and properties.get(layer.id_property) is not None:
        reduced["id"] = properties[layer.id_property]
    return reduced


def build_mask(country: dict[str, Any]) -> dict[str, Any]:
    """A world rectangle with Bangladesh punched out of it.

    GeoJSON gives a polygon's holes as the rings after the first, so one feature
    can express "everywhere except here". Every outer ring of the country
    geometry becomes a hole, islands included — dropping the small ones would
    leave dark patches over real land.

    Drawn under one translucent fill, this is what makes the dashboard read as
    Bangladesh-focused rather than as a world map that happens to be centred on
    it. Nothing about it is data: it carries no properties and no code.
    """
    holes: list[list[list[float]]] = []
    for feature in country["features"]:
        for polygon in geo.polygons(feature["geometry"]):
            # The outer ring only. A hole in Bangladesh is a hole in Bangladesh,
            # and punching it out of the mask would re-dim it.
            holes.append(polygon[0])

    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {"type": "Polygon", "coordinates": [WORLD_RING, *holes]},
            }
        ],
    }


def positions(coordinates: Any) -> Any:
    """Every ``[lon, lat]`` in an arbitrarily nested coordinate array.

    ``app.map.geometry.bounds`` is deliberately strict — it refuses anything that
    is not a boundary polygon, which is right for the database path. This layer
    also carries points and lines, so the bbox is walked structurally here rather
    than by loosening that guarantee.
    """
    if coordinates and isinstance(coordinates[0], (int, float)):
        yield coordinates
        return
    for item in coordinates:
        yield from positions(item)


def bounds_of(collection: dict[str, Any]) -> list[float]:
    """``[west, south, east, north]`` across every feature."""
    west = south = 180.0
    east = north = -180.0
    for feature in collection["features"]:
        for longitude, latitude in positions(feature["geometry"]["coordinates"]):
            west, east = min(west, longitude), max(east, longitude)
            south, north = min(south, latitude), max(north, latitude)
    return [round(west, PLACES), round(south, PLACES),
            round(east, PLACES), round(north, PLACES)]


def write(path: Path, payload: dict[str, Any]) -> int:
    """Write compactly and return the byte count.

    No indentation and no spaces after separators: this is a generated asset a
    browser parses, not a file anybody reads, and pretty-printing it costs about
    a fifth of the transfer for nothing.
    """
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE_DIR,
                        help="Directory holding the published release.")
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR,
                        help="Where the browser assets are written.")
    parser.add_argument("--check", action="store_true",
                        help="Report what would be written without writing it.")
    args = parser.parse_args(argv)

    if not args.check:
        args.output.mkdir(parents=True, exist_ok=True)

    country: dict[str, Any] | None = None
    total_source = 0
    total_output = 0
    manifest: dict[str, Any] = {}

    for layer in LAYERS:
        source_path = args.source / layer.source
        source = load(source_path)
        source_bytes = source_path.stat().st_size
        total_source += source_bytes

        features = []
        skipped = 0
        for feature in source["features"]:
            reduced = reduce_feature(feature, layer)
            if reduced is None:
                skipped += 1
                continue
            features.append(reduced)

        collection = {"type": "FeatureCollection", "features": features}
        if features:
            collection["bbox"] = bounds_of(collection)

        if layer.source == "bgd_admin0.geojson":
            country = collection

        written = (
            write(args.output / layer.output, collection)
            if not args.check
            else len(json.dumps(collection, separators=(",", ":")).encode("utf-8"))
        )
        total_output += written
        manifest[layer.output] = {
            "description": layer.description,
            "features": len(features),
            "bytes": written,
        }

        print(
            f"{layer.source:30s} {len(source['features']):5d} -> {len(features):5d} "
            f"features  {source_bytes / 1e6:7.2f} MB -> {written / 1e6:6.2f} MB"
            + (f"  ({skipped} skipped)" if skipped else "")
        )

    if country is None:  # pragma: no cover - LAYERS always contains admin0
        raise SystemExit("bgd_admin0 was not processed; the mask cannot be built.")

    mask = build_mask(country)
    mask_bytes = (
        write(args.output / "bgd_mask.geojson", mask)
        if not args.check
        else len(json.dumps(mask, separators=(",", ":")).encode("utf-8"))
    )
    total_output += mask_bytes
    manifest["bgd_mask.geojson"] = {
        "description": "World rectangle with Bangladesh punched out",
        "features": 1,
        "bytes": mask_bytes,
    }
    print(f"{'bgd_mask.geojson (derived)':30s} {'':5s}        1 feature "
          f"           -> {mask_bytes / 1e6:6.2f} MB")

    print(
        f"\nTotal {total_source / 1e6:.1f} MB -> {total_output / 1e6:.1f} MB "
        f"({total_output / total_source:.1%} of the published release)"
    )

    if args.check:
        print("\n--check: nothing written.")
        return 0

    write(args.output / "manifest.json", {
        "source": "HDX Bangladesh COD-AB v03",
        "generated_by": "scripts/build_map_geojson.py",
        "simplification": {
            "algorithm": "Douglas-Peucker",
            "coordinate_places": PLACES,
            "tolerance_degrees": {
                layer.output: layer.tolerance for layer in LAYERS
            },
        },
        "files": manifest,
    })
    print(f"Written to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
