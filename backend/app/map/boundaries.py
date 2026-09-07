"""The administrative outlines the map may draw beneath its points.

**These are not business boundaries, and the distinction is the whole reason
this module is separate from :mod:`app.map.levels`.** A division, a district
and an upazila are published administrative geography; a zone, a region, an
area and a territory are how this company organises its salespeople. Nothing
in this platform states the outline of a territory, so
``MapLevel.boundary_source`` stays ``None`` on every level and Boundary / Both
remain unavailable for all of them — exactly as ``0033``'s removal left it.

What these outlines are *for* is reference. Somebody deciding where a
territory should end needs to see the district lines their customers actually
fall inside, and a backdrop that says "this is where Gazipur is" answers that
without claiming to be where the territory ends. So a boundary set is chosen
by the reader, drawn under every business layer, and never coloured by a
metric: no fact table carries a district, so there is no figure to colour one
with, and inventing one would be the kind of number this platform refuses.

**Static files, not an endpoint.** The outlines change only when somebody
imports a new release of the source, so an API for them would answer the same
bytes on every page load. They are served from ``frontend/public/geo/`` — which
Vite copies into ``dist/`` at build time — and this module publishes only the
*catalogue*: which sets exist, what each is called, which file holds it and how
big that file is. The browser therefore holds no list of filenames, which is
the rule the top of ``CLAUDE.md`` states; ``test_map_boundaries`` pins every
declared file against what is actually on disk, so a set naming a file nobody
shipped fails the gate rather than 404-ing in front of a reader.

The source is HDX's Bangladesh COD-AB v03, Douglas–Peucker simplified at a
per-level tolerance and rounded to five decimal places.
``frontend/public/geo/manifest.json`` records the provenance beside the files.

**The files were restored from the pre-removal tree, not regenerated**, and are
byte-identical to the ones ``4ee51b7`` deleted. The generator that produced them
(``scripts/build_map_geojson.py``) went with that removal and is *not* back: it
imports ``app.map.geometry``, which went too, so restoring the script alone
would ship one that fails on import — worse than none. Naming it here anyway
would be the failure the top of ``CLAUDE.md`` opens with, one directory over.

The practical consequence is worth stating rather than discovering: a new COD-AB
release cannot be processed until the generator and the simplifier come back,
which is its own piece of work. Until then these five files are the geography,
and they are committed rather than built for that reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..database.models_map import DesignPurpose

#: Where the browser fetches a boundary file from. A URL path, not a directory:
#: the files are static assets served by nginx (or by Vite in development), and
#: nothing on the backend reads them.
PATH_PREFIX = "/geo/"

#: Dims everything outside the country so the eye goes to the data. Decoration
#: rather than a boundary set — it is not selectable and carries no codes.
MASK_FILE = "bgd_mask.geojson"


@dataclass(frozen=True)
class BoundarySet:
    """One administrative level's outlines, as a file the browser may fetch."""

    key: str
    label: str
    file: str
    #: The administrative depth, 0 being the country. Kept because the source
    #: names its files this way and a reader of the manifest needs the link.
    admin_level: int
    #: The master whose rows these outlines correspond to, where one exists.
    #: ``dim_country`` / ``dim_division`` / ``dim_district`` / ``dim_upazila``
    #: all survived ``0033`` as master data, so a selected outline can be tied
    #: back to a record. ``None`` would mean an outline with nothing behind it.
    table: str
    #: How many outlines the file holds, and how many bytes it is. Published so
    #: the control can warn before pulling 1.7 MB over a slow connection rather
    #: than appearing to hang.
    features: int
    bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "url": f"{PATH_PREFIX}{self.file}",
            "file": self.file,
            "admin_level": self.admin_level,
            "table": self.table,
            "features": self.features,
            "bytes": self.bytes,
        }


#: Every set a reader may switch on, coarsest first.
#:
#: The counts and sizes are restated here rather than read from the manifest at
#: run time, because the backend must not depend on a frontend build artefact
#: being present to answer a request. They are pinned against the real files by
#: ``test_map_boundaries``, which is what stops the two drifting.
#:
#: The country outline is deliberately included: on its own it is the least
#: useful of the four, but it is what makes the mask meaningful and it costs
#: 40 KB.
BOUNDARY_SETS: tuple[BoundarySet, ...] = (
    BoundarySet(key="country", label="Country", file="bgd_admin0.geojson",
                admin_level=0, table="dim_country", features=1, bytes=40_867),
    BoundarySet(key="division", label="Divisions", file="bgd_admin1.geojson",
                admin_level=1, table="dim_division", features=8, bytes=95_337),
    BoundarySet(key="district", label="Districts", file="bgd_admin2.geojson",
                admin_level=2, table="dim_district", features=64, bytes=369_934),
    BoundarySet(key="upazila", label="Upazilas", file="bgd_admin3.geojson",
                admin_level=3, table="dim_upazila", features=507,
                bytes=1_727_737),
)

BOUNDARY_BY_KEY: dict[str, BoundarySet] = {s.key: s for s in BOUNDARY_SETS}
BOUNDARY_KEYS: tuple[str, ...] = tuple(BOUNDARY_BY_KEY)

#: What each map opens with. **Both open with upazilas.**
#:
#: Area Demarcation always did, and for a reason that has not changed: there
#: the outlines are not context over the subject, they *are* the subject. That
#: tab exists to judge where a line falls, and a reader who has to switch the
#: backdrop on before the tab can answer its own question has been given a
#: blank map and a chore. Upazila rather than district because a territory is
#: drawn at roughly upazila grain; districts are too coarse to place a boundary
#: against.
#:
#: **The Business Map now does too, which reverses what this comment used to
#: say.** The old reasoning was that a backdrop nobody asked for is a download
#: and a lot of ink over the points somebody came to look at — true of a map
#: that draws figures, where the outlines would be context laid over its
#: subject. The judgement that overrides it is that a regional figure is read
#: together with the ground it covers, and a reader who wants that had to find
#: a control first: an opt-in default asks every reader to do the work for the
#: ones who do not want it. The ink half was also weaker than it read, since
#: :data:`BOUNDARY_STYLE` draws the fill at 0.06 opacity and the line at 0.55
#: precisely so the outlines recede behind whatever sits on top of them.
#:
#: The cost is accepted rather than overlooked, and it is now paid on both
#: tabs: ``bgd_admin3.geojson`` is 1.7 MB and 507 features. It is fetched once
#: per session (``geoData`` caches the promise), it never blocks the points —
#: those come from a different request and draw first — and the wait is stated
#: while it happens, which is what ``BoundaryControl``'s loading state is for.
#: ``boundary=none`` still turns it off and is still spelled out, so a reader
#: who does not want it says so once and is not overruled on the next reload.
#:
#: **Still keyed by purpose although both answers now agree**, deliberately.
#: The shape is what ``catalogue()`` publishes and what the browser reads per
#: tab; the two are free to diverge again; and :func:`_assert_defaults_resolve`
#: goes on earning its keep either way. Collapsing it back to one constant
#: would be churn to undo the first time one surface wants what the other does
#: not — which is the state this map was in a week ago.
DEFAULT_BOUNDARY_BY_PURPOSE: dict[str, str | None] = {
    DesignPurpose.ANALYSIS: "upazila",
    DesignPurpose.DEMARCATION: "upazila",
}


def _assert_defaults_resolve() -> None:
    """A default naming a set nobody ships would read as "none", in silence.

    Checked at import so a typo fails the gate rather than quietly turning the
    demarcation tab back into the blank map this change exists to end. The
    purposes are checked too: a key that is not a real ``DesignPurpose`` would
    never be looked up, so the default would simply never apply.
    """
    for purpose, key in DEFAULT_BOUNDARY_BY_PURPOSE.items():
        if purpose not in DesignPurpose.ALL:
            raise ValueError(
                f"DEFAULT_BOUNDARY_BY_PURPOSE names {purpose!r}, which is not a "
                f"map purpose. Known: {', '.join(DesignPurpose.ALL)}."
            )
        if key is not None and key not in BOUNDARY_BY_KEY:
            raise ValueError(
                f"DEFAULT_BOUNDARY_BY_PURPOSE[{purpose!r}] is {key!r}, which is "
                f"not a boundary set. Known: {', '.join(BOUNDARY_KEYS)}."
            )
    missing = set(DesignPurpose.ALL) - set(DEFAULT_BOUNDARY_BY_PURPOSE)
    if missing:
        raise ValueError(
            f"DEFAULT_BOUNDARY_BY_PURPOSE says nothing about {', '.join(sorted(missing))}. "
            f"Every map has to open with something, including nothing."
        )


_assert_defaults_resolve()


def get_boundary(key: str) -> BoundarySet:
    """Look up a set, raising a clear error for an unknown key."""
    found = BOUNDARY_BY_KEY.get(key)
    if found is None:
        raise ValueError(
            f"Unknown boundary set {key!r}. Supported: {', '.join(BOUNDARY_KEYS)}."
        )
    return found


def catalogue() -> dict[str, Any]:
    """The catalogue as ``GET /api/map/config`` publishes it."""
    return {
        "sets": [boundary.to_dict() for boundary in BOUNDARY_SETS],
        # Keyed by purpose, and replacing the single `default` this used to
        # publish rather than sitting beside it: a scalar that cannot express
        # "none here, upazilas there" is the thing being fixed, and leaving it
        # in place would leave a second answer for anything that still read it.
        "defaults": dict(DEFAULT_BOUNDARY_BY_PURPOSE),
        "mask_url": f"{PATH_PREFIX}{MASK_FILE}",
        # Said in the payload, not only in this module's docstring, because the
        # browser is where somebody will next be tempted to colour one by a
        # metric.
        "note": (
            "Administrative reference outlines. They are not business "
            "boundaries: no source states the outline of a zone, region, area "
            "or territory, and none is drawn."
        ),
    }


__all__ = [
    "BOUNDARY_BY_KEY",
    "BOUNDARY_KEYS",
    "BOUNDARY_SETS",
    "DEFAULT_BOUNDARY_BY_PURPOSE",
    "MASK_FILE",
    "PATH_PREFIX",
    "BoundarySet",
    "catalogue",
    "get_boundary",
]
