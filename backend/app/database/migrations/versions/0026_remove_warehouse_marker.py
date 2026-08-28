"""The Warehouse marker design, left behind when Warehouse itself was removed.

Revision ID: 0026_remove_warehouse_marker
Revises: 0025_agent_learning
Create Date: 2026-08-25

Revision 0020 removed the Warehouse dataset from the platform — the dimension,
the facts, the columns and the views. What it did not remove was the *marker
design* seeded for the warehouse entity type, because that row lives in the map
configuration rather than in the warehouse schema and nothing joined the two.

So a "Warehouse Default" design has sat in the Marker Library ever since,
naming an entity type ``app.map.entities.ENTITY_TYPES`` no longer contains. It
is precisely the failure CLAUDE.md warns about — a name outliving what it named
— and it is visible: the design is listed on the Map Settings page for a layer
the map can no longer draw, and it cannot be removed from there, because the
delete control is disabled for a system default and this row is one.

**Why a migration rather than a delete on one database.** Every deployment that
was seeded before 0020 has this row; the seeding walked the entity-type list of
its day. Fixing it in one place would leave every other copy showing a layer
that does not exist. The seeding will *not* put it back — ``seed_map_defaults``
walks ``ENTITY_TYPES``, and warehouse is not in it — so this is a one-way
correction with nothing to re-suppress afterwards.

**What is removed, and what is checked first.** The design and its stored
versions, and nothing else. The revision counts an assignment pointing at the
design before touching anything and **aborts** if it finds one, the way 0020
counts every object it is about to drop: an assignment would mean some entity
type is still drawn with this design, which would make the row load-bearing and
this revision wrong. Rows were exported to
``reports/map_marker_design_warehouse_pre0026.csv`` (and the versions beside it)
before this ran, and the design's own definition is printed to the migration log
as it goes, because a design is configuration somebody authored and losing it
silently is not the same as losing a seeded default.

Nothing else in the map configuration referenced warehouse — the styles, the
boundaries, the entity locations and the admin points were all checked and are
clean.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026_remove_warehouse_marker"
down_revision: Union[str, None] = "0025_agent_learning"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The entity type that no longer exists.
GONE = "warehouse"


def _scalar(bind, statement: str, **params) -> int:
    return bind.execute(sa.text(statement), params).scalar() or 0


def _table_exists(bind, name: str) -> bool:
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    bind = op.get_bind()

    if not _table_exists(bind, "map_marker_designs"):
        return

    designs = _scalar(
        bind,
        "SELECT count(*) FROM map_marker_designs WHERE entity_type = :type",
        type=GONE,
    )
    if not designs:
        # Already clean — a fresh database seeded after 0020 never had one.
        return

    # An assignment would mean something is still drawn with this design, which
    # would make it load-bearing rather than orphaned. Counted before anything
    # is deleted, and fatal: aborting with the row intact is recoverable, and
    # deleting a design something still points at is not.
    if _table_exists(bind, "map_marker_assignments"):
        assigned = _scalar(
            bind,
            "SELECT count(*) FROM map_marker_assignments a "
            "JOIN map_marker_designs d ON d.design_id = a.design_id "
            "WHERE d.entity_type = :type",
            type=GONE,
        )
        if assigned:
            raise RuntimeError(
                f"{assigned} marker assignment(s) still point at a '{GONE}' "
                "design. That entity type was removed in revision 0020, so an "
                "assignment to it should not exist — reassign or remove those "
                "rows and re-run. Nothing has been deleted."
            )

    # Logged before the delete, so the authored definition survives in the
    # migration output even where the CSV export was skipped.
    for row in bind.execute(sa.text(
        "SELECT design_id, name, status, version, definition "
        "FROM map_marker_designs WHERE entity_type = :type"
    ), {"type": GONE}).mappings():
        print(
            f"  removing marker design {row['design_id']} '{row['name']}' "
            f"(status={row['status']}, version={row['version']}): "
            f"{str(row['definition'])[:200]}"
        )

    versions = 0
    if _table_exists(bind, "map_marker_design_versions"):
        versions = _scalar(
            bind,
            "SELECT count(*) FROM map_marker_design_versions v "
            "JOIN map_marker_designs d ON d.design_id = v.design_id "
            "WHERE d.entity_type = :type",
            type=GONE,
        )
        # Children first: the version rows carry the foreign key, so they go
        # before the design they reference rather than relying on a cascade the
        # schema may not declare.
        op.execute(sa.text(
            "DELETE FROM map_marker_design_versions WHERE design_id IN ("
            "SELECT design_id FROM map_marker_designs WHERE entity_type = :type)"
        ).bindparams(type=GONE))

    op.execute(sa.text(
        "DELETE FROM map_marker_designs WHERE entity_type = :type"
    ).bindparams(type=GONE))

    print(
        f"  NOTE: removed {designs} '{GONE}' marker design(s) and {versions} "
        "stored version(s). The entity type went in revision 0020; the design "
        "outlived it because map configuration is not part of the warehouse "
        "schema. Seeding walks ENTITY_TYPES and will not recreate it."
    )


def downgrade() -> None:
    """Nothing comes back, and inventing it would be worse than the gap.

    The design was configuration somebody had edited — its stored version was 3,
    not the seeded 1 — so recreating it would mean fabricating a definition this
    revision has no way to know. The exported CSV in ``reports/`` is the copy;
    restoring from it is a deliberate act with a real file behind it, not
    something a downgrade should guess at.

    Leaving this empty is also the honest description of the state: the entity
    type this design named does not exist on either side of the downgrade, so a
    database without the row is correct at 0025 just as it is at 0026.
    """
