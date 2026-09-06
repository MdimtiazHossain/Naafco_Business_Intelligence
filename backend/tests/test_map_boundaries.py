"""The administrative outlines the map may draw, and what they must never claim.

The catalogue in :mod:`app.map.boundaries` restates each file's name, feature
count and size rather than reading them from the manifest at run time — the
backend must not need a frontend build artefact present to answer a request.
The cost of that is a second copy, and these tests are what stop it drifting:
every declared file is checked against the real one on disk.

The other half is the distinction the module exists for. A division is not a
sales region, so no business level may gain a boundary source and no metric may
colour an outline.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from app.map import boundaries, levels, styles

GEO_DIR = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "public" / "geo"


# ==========================================================================
# The catalogue matches what was actually shipped
# ==========================================================================


@pytest.mark.parametrize("boundary", boundaries.BOUNDARY_SETS,
                         ids=lambda b: b.key)
def test_every_declared_set_names_a_file_that_exists(boundary):
    """A catalogue entry naming a missing file 404s in front of a reader.

    The whole point of declaring these server-side is that the browser holds no
    list of filenames. That only helps if the server's list is true.
    """
    path = GEO_DIR / boundary.file
    assert path.exists(), f"{boundary.key} names {boundary.file}, which is not shipped"


@pytest.mark.parametrize("boundary", boundaries.BOUNDARY_SETS,
                         ids=lambda b: b.key)
def test_every_declared_count_and_size_is_the_real_one(boundary):
    """Restated numbers, pinned against the file rather than trusted.

    The size is published so the control can warn before pulling 1.7 MB over a
    slow connection; a stale figure would make that warning a lie.
    """
    path = GEO_DIR / boundary.file
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data["features"]) == boundary.features, boundary.key
    assert path.stat().st_size == boundary.bytes, boundary.key


@pytest.mark.parametrize("boundary", boundaries.BOUNDARY_SETS,
                         ids=lambda b: b.key)
def test_every_outline_carries_a_code_and_a_name(boundary):
    """A selected outline has to be able to say what it is.

    Without a code there is nothing to tie the polygon back to the master its
    ``table`` names, and the click would select something anonymous.
    """
    data = json.loads((GEO_DIR / boundary.file).read_text(encoding="utf-8"))
    for feature in data["features"][:20]:
        assert feature["properties"].get("code"), boundary.key
        assert feature["properties"].get("name"), boundary.key
        assert feature["geometry"]["type"] in ("Polygon", "MultiPolygon")


def test_the_mask_is_shipped_but_is_not_a_selectable_set():
    """Decoration, not geography: it has no codes and nothing to select."""
    assert (GEO_DIR / boundaries.MASK_FILE).exists()
    assert boundaries.MASK_FILE not in {s.file for s in boundaries.BOUNDARY_SETS}


def test_the_manifest_describes_exactly_what_is_shipped():
    """A manifest naming files nobody shipped is the stale list, in JSON.

    The original described eight files; five were restored, so it was rewritten
    rather than restored verbatim.
    """
    manifest = json.loads((GEO_DIR / "manifest.json").read_text(encoding="utf-8"))
    described = set(manifest["files"])
    on_disk = {p.name for p in GEO_DIR.glob("*.geojson")}
    assert described == on_disk, "the manifest and the directory disagree"
    for name, meta in manifest["files"].items():
        assert (GEO_DIR / name).stat().st_size == meta["bytes"], name


# ==========================================================================
# What they are not
# ==========================================================================


def test_no_business_level_gains_a_boundary_source():
    """The rule ``0033`` recorded, and the reason this module is separate.

    Restoring administrative outlines must not make Boundary or Both selectable
    for a zone, a region, an area or a territory. Nothing states where a
    territory ends; a district line near it is context, not an answer.
    """
    for level in levels.MAP_LEVELS:
        assert level.boundary_source is None, level.key
        assert level.boundary_available is False, level.key
        assert list(levels.view_modes_for(level)) == ["point"], level.key


def test_a_boundary_set_is_not_a_map_level():
    """The two catalogues stay apart, so neither can be drawn as the other."""
    assert not set(boundaries.BOUNDARY_KEYS) & set(levels.LEVEL_KEYS)


def test_the_catalogue_says_out_loud_what_these_are_not():
    """Stated in the payload, not only in a docstring the browser cannot read."""
    note = boundaries.catalogue()["note"]
    assert "not business boundaries" in note.lower()


def test_nothing_opens_with_a_backdrop_switched_on():
    """370 KB and a lot of ink nobody asked for."""
    assert boundaries.DEFAULT_BOUNDARY is None
    assert boundaries.catalogue()["default"] is None


def test_the_boundary_style_is_declared_once_and_published():
    """The renderer invents no colour of its own, here as everywhere else."""
    style = styles.default_style()["boundary"]
    assert style == styles.BOUNDARY_STYLE
    # Quiet by construction: a backdrop that competed with the points would be
    # read as meaning, and no metric colours these.
    assert style["fill_opacity"] < 0.2
    assert style["selected_fill_opacity"] > style["fill_opacity"]


def test_an_unknown_set_is_refused_by_name():
    with pytest.raises(ValueError) as raised:
        boundaries.get_boundary("thana")
    assert "thana" in str(raised.value)
    assert ", ".join(boundaries.BOUNDARY_KEYS) in str(raised.value)
