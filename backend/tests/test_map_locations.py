"""The rebuilt map's foundation: coordinates, derivation and composition.

Revision ``0034_business_map`` recreates ``map_entity_locations`` and the three
composition tables. These tests cover what a coordinate may be, how the levels
above the placed ones are derived, how an exported table is restored, how
coordinates arrive through the Upload Centre, and what the migration seeds.
"""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_session
from app.auth.security import hash_password
from app.database.models import DimSubTerritory, DimTerritory
from app.database.models_ai import AppUser, Role, UserStatus
from app.database.models_map import (
    DesignPurpose,
    GeoPrecision,
    GeoSource,
    LayerViewMode,
    MapDesign,
    MapEntityLocation,
    MapLayer,
    MapPointConfiguration,
)
from app.database.models_warehouse import DimCustomer
from app.main import app
from app.map import geo, levels

pytest.importorskip("multipart", reason="python-multipart is required by FastAPI forms")

PASSWORD = "Correct-Horse-9"

#: Real positions, so a wrong projection would be visible rather than plausible.
DHAKA = (23.7808, 90.4008)
KHULNA = (22.8456, 89.5403)


def placed(session: Session, entity_type: str, code: str) -> MapEntityLocation | None:
    return session.execute(
        select(MapEntityLocation).where(
            MapEntityLocation.entity_type == entity_type,
            MapEntityLocation.entity_code == code,
        )
    ).scalar_one_or_none()


def add_customers(session: Session, *specs: tuple[str, str, str]) -> None:
    """``(code, name, sub_territory_code)`` rows in the customer master."""
    for code, name, sub_territory in specs:
        session.add(DimCustomer(customer_code=code, customer_name=name,
                                sub_territory_code=sub_territory))
    session.flush()


# ==========================================================================
# The migration
# ==========================================================================


def test_the_migration_seeds_one_protected_default_design(warehouse_engine):
    """One analysis design, protected. ``0035`` seeds a demarcation one beside
    it, which is a different map — hence the filter rather than a bare count."""
    with Session(warehouse_engine) as session:
        designs = session.execute(
            select(MapDesign).where(MapDesign.purpose == DesignPurpose.ANALYSIS)
        ).scalars().all()
        assert len(designs) == 1
        design = designs[0]
        assert design.name == "Business Overview"
        assert design.is_default and design.is_active and design.is_system_default
        assert design.default_metric == "net_sales"

        layers = design.layers
        assert [layer.point_level for layer in layers] == [
            "zone", "region", "area", "unit", "territory", "sub_territory",
            "customer",
        ], "every specified level, in order, with Unit between Area and Territory"
        assert [layer.display_order for layer in layers] == list(range(1, 8))
        # Unit and Customer ship hidden; the five the specification pictures
        # are on.
        assert {layer.point_level for layer in layers if not layer.is_visible} == {
            "unit", "customer",
        }
        assert all(layer.view_mode == LayerViewMode.POINT for layer in layers)
        assert all(layer.metric is None for layer in layers), \
            "a seeded layer inherits the design's metric"
        assert {layer.color_metric for layer in layers} == {"achievement"}
        assert {layer.size_metric for layer in layers} == {"net_sales"}
        # Customer clusters earliest and appears last.
        customer = next(layer for layer in layers if layer.point_level == "customer")
        assert customer.cluster_at == 200 and customer.min_zoom == 9

        configs = session.execute(
            select(MapPointConfiguration)
            .where(MapPointConfiguration.layer_id.in_(
                [layer.layer_id for layer in layers]))
        ).scalars().all()
        assert {config.layer_id for config in configs} == {l.layer_id for l in layers}
        assert all(config.label_field == "name" and not config.show_label
                   for config in configs)


def test_a_new_design_does_not_collide_with_the_seed(warehouse_engine):
    """The seed carries no primary keys, so the next insert gets a fresh one.

    0032 seeded with explicit ids, which on PostgreSQL leaves the identity
    sequence at zero and makes the first design an administrator creates
    collide with the seed. SQLite would not show that, so what this pins is the
    weaker property both dialects share: the seed's keys were assigned by the
    database, and a second design and its layer land beside them.
    """
    with Session(warehouse_engine) as session:
        before_designs = session.execute(
            select(func.count()).select_from(MapDesign)).scalar_one()
        before_layers = session.execute(
            select(func.count()).select_from(MapLayer)).scalar_one()
        design = MapDesign(name="Territory Performance", created_by="tester")
        design.layers.append(MapLayer(layer_name="Territory",
                                      point_level="territory", display_order=1))
        session.add(design)
        session.commit()
        # Counted rather than hard-coded: 0034 seeds one design and 0035 a
        # second, and a test that names the next id has to be edited by every
        # migration that seeds anything. What matters is that the database
        # assigned the key, not what number it reached.
        assert design.design_id > before_designs
        assert design.layers[0].layer_id > before_layers


def test_deleting_a_design_takes_its_layers_and_configurations(warehouse_engine):
    with Session(warehouse_engine) as session:
        session.execute(__import__("sqlalchemy").text("PRAGMA foreign_keys=ON"))
        design = MapDesign(name="Scratch", created_by="tester")
        layer = MapLayer(layer_name="Zone", point_level="zone", display_order=1)
        layer.point_config = MapPointConfiguration(label_field="name")
        design.layers.append(layer)
        session.add(design)
        session.commit()
        layer_id = layer.layer_id

        session.delete(design)
        session.commit()
        assert session.execute(
            select(MapLayer).where(MapLayer.layer_id == layer_id)
        ).scalar_one_or_none() is None
        assert session.execute(
            select(MapPointConfiguration)
            .where(MapPointConfiguration.layer_id == layer_id)
        ).scalar_one_or_none() is None


def test_one_layer_per_level_per_design(warehouse_engine):
    from sqlalchemy.exc import IntegrityError

    with Session(warehouse_engine) as session:
        design = session.execute(
            select(MapDesign).where(MapDesign.purpose == DesignPurpose.ANALYSIS)
        ).scalar_one()
        design.layers.append(MapLayer(layer_name="Zone again", point_level="zone",
                                      display_order=9))
        with pytest.raises(IntegrityError):
            session.commit()


# ==========================================================================
# Levels
# ==========================================================================


def test_every_specified_level_is_drawable_and_derived_from_the_chain():
    from app.org.hierarchy import ORG_CHAIN

    keys = [level.key for level in levels.MAP_LEVELS]
    assert keys == [*ORG_CHAIN, "customer", "sales_force"]
    for spec_level in ("zone", "region", "area", "territory", "sub_territory",
                       "customer"):
        assert levels.get_level(spec_level).promoted, spec_level
    # Unit is promoted too: it is a rung of this platform's chain.
    assert levels.get_level("unit").promoted
    assert not levels.get_level("company").promoted

    sub_territory = levels.get_level("sub_territory")
    assert sub_territory.model is DimSubTerritory
    assert sub_territory.parent == "territory"
    assert sub_territory.parent_code_field == "territory_code"
    assert levels.get_level("customer").parent == "sub_territory"
    assert levels.get_level("sales_force").parent == "territory"

    with pytest.raises(ValueError, match="Unknown map level"):
        levels.get_level("wormhole")


# ==========================================================================
# Coordinates
# ==========================================================================


@pytest.mark.parametrize("latitude,longitude", [
    (0, 0), (95, 90), (23, 200), ("north", 90), (float("nan"), 90),
])
def test_an_impossible_coordinate_is_refused(latitude, longitude):
    with pytest.raises(geo.InvalidCoordinate):
        geo.validate(latitude, longitude)


def test_a_valid_coordinate_is_rounded_not_rewritten():
    assert geo.validate("23.78081234567", 90.40084) == (23.780812, 90.40084)


def test_centroid_handles_the_antimeridian():
    """A degree average would give 0; the correct answer is ±180."""
    latitude, longitude = geo.centroid([(0.0, 179.0), (0.0, -179.0)])
    assert latitude == 0.0
    assert abs(abs(longitude) - 180.0) < 0.001


def test_bounds_enclose_and_pad():
    bounds = geo.bounds_of([DHAKA, KHULNA])
    assert bounds is not None
    assert bounds.north == DHAKA[0] and bounds.south == KHULNA[0]
    padded = bounds.padded()
    assert padded.north > bounds.north and padded.west < bounds.west
    assert geo.bounds_of([]) is None


def test_a_coordinate_needs_a_known_level(seeded_engine):
    with Session(seeded_engine) as session:
        with pytest.raises(ValueError, match="Unknown map level"):
            geo.upsert_location(session, entity_type="wormhole", entity_code="X",
                                latitude=DHAKA[0], longitude=DHAKA[1])


def test_placing_customers_derives_every_level_above(seeded_engine):
    """Place the level where an address exists; the rest follows."""
    with Session(seeded_engine) as session:
        add_customers(session, ("CUST-A", "Dhiren & Brothers", "STR001"),
                      ("CUST-B", "Anis & Sons", "STR001"))
        geo.upsert_location(session, entity_type="customer", entity_code="CUST-A",
                            latitude=23.70, longitude=90.40)
        geo.upsert_location(session, entity_type="customer", entity_code="CUST-B",
                            latitude=23.90, longitude=90.40)
        derived = geo.derive_parents(session, actor="tester")
        session.commit()

        # sub-territory (from customers) -> territory -> unit -> area -> region
        # -> zone -> sales line -> bu -> company.
        assert derived["sub_territory"] == 1
        assert {"territory", "unit", "area", "region", "zone"} <= set(derived)

        sub_territory = placed(session, "sub_territory", "STR001")
        assert sub_territory is not None
        assert sub_territory.source == GeoSource.DERIVED
        assert sub_territory.precision == GeoPrecision.CENTROID
        assert sub_territory.derived_from == 2
        assert round(sub_territory.latitude, 3) == 23.8
        assert sub_territory.updated_by == "tester"

        region = placed(session, "region", "REG001")
        assert region is not None and region.derived_from == 1
        assert round(region.latitude, 3) == 23.8
        # The other region has no placed child and is left unplaced, not zeroed.
        assert placed(session, "region", "REG002") is None


def test_a_hand_placed_coordinate_survives_re_derivation(seeded_engine):
    """A derived centroid must never overwrite a decision someone made."""
    with Session(seeded_engine) as session:
        add_customers(session, ("CUST-A", "Dhiren & Brothers", "STR001"))
        geo.upsert_location(session, entity_type="customer", entity_code="CUST-A",
                            latitude=DHAKA[0], longitude=DHAKA[1])
        geo.upsert_location(session, entity_type="region", entity_code="REG001",
                            latitude=24.0, longitude=91.0, source=GeoSource.MANUAL)
        derived = geo.derive_parents(session)
        session.commit()

        assert "region" not in derived
        region = placed(session, "region", "REG001")
        assert (region.latitude, region.longitude) == (24.0, 91.0)
        assert region.source == GeoSource.MANUAL
        # Levels above the hand-placed one are still derived, through it.
        zone = placed(session, "zone", "Z001")
        assert zone is not None and zone.source == GeoSource.DERIVED
        assert (zone.latitude, zone.longitude) == (24.0, 91.0)


def test_re_deriving_moves_a_centroid_when_its_children_move(seeded_engine):
    with Session(seeded_engine) as session:
        add_customers(session, ("CUST-A", "Dhiren & Brothers", "STR001"))
        geo.upsert_location(session, entity_type="customer", entity_code="CUST-A",
                            latitude=DHAKA[0], longitude=DHAKA[1])
        geo.derive_parents(session)
        geo.upsert_location(session, entity_type="customer", entity_code="CUST-A",
                            latitude=KHULNA[0], longitude=KHULNA[1])
        geo.derive_parents(session)
        session.commit()
        territory = placed(session, "territory", "TR001")
        assert (territory.latitude, territory.longitude) == KHULNA


def test_a_sales_force_placement_seeds_its_territory_until_a_finer_one_exists(
        seeded_engine):
    from app.database.models_warehouse import DimSalesForce

    with Session(seeded_engine) as session:
        session.add(DimSalesForce(sales_force_code="SF-1", sales_force_name="Rep",
                                  territory_code="TR001"))
        session.flush()
        geo.upsert_location(session, entity_type="sales_force", entity_code="SF-1",
                            latitude=KHULNA[0], longitude=KHULNA[1])
        derived = geo.derive_parents(session)
        assert derived["territory"] == 1
        territory = placed(session, "territory", "TR001")
        assert (territory.latitude, territory.longitude) == KHULNA

        # A placed customer under the same territory is the finer placement
        # and replaces the sales-force centroid.
        add_customers(session, ("CUST-A", "Dhiren & Brothers", "STR001"))
        geo.upsert_location(session, entity_type="customer", entity_code="CUST-A",
                            latitude=DHAKA[0], longitude=DHAKA[1])
        geo.derive_parents(session)
        territory = placed(session, "territory", "TR001")
        assert (territory.latitude, territory.longitude) == DHAKA


def test_a_parent_code_the_master_lacks_is_not_placed(seeded_engine):
    """A centroid for a code that is not an entity is a position for nothing.

    The deployment's sales-force master names sub-territory codes in its
    ``territory_code`` column; grouping on it must not manufacture territories.
    """
    from app.database.models_warehouse import DimSalesForce

    with Session(seeded_engine) as session:
        session.add(DimSalesForce(sales_force_code="SF-1", sales_force_name="Rep",
                                  territory_code="STR001"))   # a sub-territory
        session.flush()
        geo.upsert_location(session, entity_type="sales_force", entity_code="SF-1",
                            latitude=KHULNA[0], longitude=KHULNA[1])
        derived = geo.derive_parents(session)
        assert "territory" not in derived
        assert placed(session, "territory", "STR001") is None
        assert placed(session, "sub_territory", "STR001") is None,             "and it is not quietly re-filed at the level the code belongs to"


def test_an_orphaned_centroid_is_removed_on_re_derivation(seeded_engine):
    with Session(seeded_engine) as session:
        session.add(MapEntityLocation(entity_type="territory", entity_code="TR-GONE",
                                      latitude=23.0, longitude=90.0,
                                      source=GeoSource.DERIVED,
                                      precision=GeoPrecision.CENTROID))
        session.add(MapEntityLocation(entity_type="territory", entity_code="TR-KEPT",
                                      latitude=23.0, longitude=90.0,
                                      source=GeoSource.MANUAL,
                                      precision=GeoPrecision.EXACT))
        session.flush()
        geo.derive_parents(session)
        assert placed(session, "territory", "TR-GONE") is None
        # A coordinate a person placed is theirs to remove, code or no code.
        assert placed(session, "territory", "TR-KEPT") is not None


def test_a_centroid_goes_when_the_coordinates_it_was_computed_from_do(
        seeded_engine):
    """The other way a centroid's inputs disappear.

    ``_prune_orphaned_centroids`` catches an entity the master data dropped.
    This catches an entity that still exists with nothing placed below it any
    more, which is what removing the last customer coordinate under a
    sub-territory does. Both leave a position nothing supports. It cascades,
    because a territory derived from that sub-territory is standing on the same
    nothing — and the prune runs per level as the pass climbs precisely so the
    parent is not re-derived from a centroid removed later in the same pass.
    """
    with Session(seeded_engine) as session:
        add_customers(session, ("CUST-A", "Dhiren & Brothers", "STR001"))
        geo.upsert_location(session, entity_type="customer", entity_code="CUST-A",
                            latitude=DHAKA[0], longitude=DHAKA[1])
        geo.derive_parents(session)
        assert placed(session, "sub_territory", "STR001") is not None
        assert placed(session, "territory", "TR001") is not None

        session.delete(placed(session, "customer", "CUST-A"))
        session.flush()
        geo.derive_parents(session)

        assert placed(session, "sub_territory", "STR001") is None
        assert placed(session, "territory", "TR001") is None


def test_re_deriving_changes_nothing_when_nothing_below_has_moved(seeded_engine):
    """The prune must remove stale rows without touching supported ones.

    Guards the obvious way to get the new pruning wrong — a level whose codes
    were not recorded during the pass would have every one of its centroids
    deleted and rebuilt, or deleted and not rebuilt.
    """
    with Session(seeded_engine) as session:
        add_customers(session, ("CUST-A", "Dhiren & Brothers", "STR001"),
                      ("CUST-B", "Anis & Sons", "STR001"))
        for code, (lat, lon) in (("CUST-A", DHAKA), ("CUST-B", KHULNA)):
            geo.upsert_location(session, entity_type="customer", entity_code=code,
                                latitude=lat, longitude=lon)
        geo.derive_parents(session)
        session.flush()
        first = {(r.entity_type, r.entity_code): (r.latitude, r.longitude)
                 for r in session.execute(select(MapEntityLocation)).scalars()}

        geo.derive_parents(session)
        session.flush()
        second = {(r.entity_type, r.entity_code): (r.latitude, r.longitude)
                  for r in session.execute(select(MapEntityLocation)).scalars()}

    assert first == second
    assert first, "the fixture should have produced some coordinates"


def test_coverage_separates_placed_from_derived(seeded_engine):
    with Session(seeded_engine) as session:
        add_customers(session, ("CUST-A", "Dhiren & Brothers", "STR001"),
                      ("CUST-B", "Anis & Sons", "STR001"))
        geo.upsert_location(session, entity_type="customer", entity_code="CUST-A",
                            latitude=DHAKA[0], longitude=DHAKA[1])
        geo.derive_parents(session)
        rows = {row["entity_type"]: row for row in geo.coverage(session)}
        assert rows["customer"] == {
            "entity_type": "customer", "label": "Customer", "total": 2,
            "placed": 1, "derived": 0, "missing": 1,
        }
        assert rows["sub_territory"]["placed"] == 1
        assert rows["sub_territory"]["derived"] == 1
        assert rows["territory"]["total"] == session.execute(
            select(__import__("sqlalchemy").func.count())
            .select_from(DimTerritory.__table__)).scalar_one()


# ==========================================================================
# Restoring the pre-0033 export
# ==========================================================================


def export_rows() -> list[dict[str, str]]:
    """The shape ``reports/map_pre0033_*/map_entity_locations.csv`` has."""
    return [
        {"location_id": "1", "entity_type": "customer", "entity_code": "CUST-A",
         "latitude": "23.7808", "longitude": "90.4008", "source": "UPLOAD",
         "precision": "APPROXIMATE", "label": "Dhiren & Brothers",
         "derived_from": "", "updated_by": "", "created_at": "", "updated_at": ""},
        {"location_id": "2", "entity_type": "customer", "entity_code": "CUST-B",
         "latitude": "22.8456", "longitude": "89.5403", "source": "UPLOAD",
         "precision": "APPROXIMATE", "label": "", "derived_from": "",
         "updated_by": "", "created_at": "", "updated_at": ""},
        # A derived centroid: skipped, then recomputed.
        {"location_id": "3", "entity_type": "sub_territory", "entity_code": "STR001",
         "latitude": "1.0", "longitude": "1.0", "source": "DERIVED",
         "precision": "CENTROID", "label": "", "derived_from": "2",
         "updated_by": "", "created_at": "", "updated_at": ""},
        # A customer the master no longer holds.
        {"location_id": "4", "entity_type": "customer", "entity_code": "CUST-GONE",
         "latitude": "23.0", "longitude": "90.0", "source": "UPLOAD",
         "precision": "APPROXIMATE", "label": "", "derived_from": "",
         "updated_by": "", "created_at": "", "updated_at": ""},
        # A level that does not exist.
        {"location_id": "5", "entity_type": "warehouse", "entity_code": "WH1",
         "latitude": "23.0", "longitude": "90.0", "source": "UPLOAD",
         "precision": "APPROXIMATE", "label": "", "derived_from": "",
         "updated_by": "", "created_at": "", "updated_at": ""},
        # A coordinate that was never a place.
        {"location_id": "6", "entity_type": "region", "entity_code": "REG002",
         "latitude": "0", "longitude": "0", "source": "MANUAL",
         "precision": "EXACT", "label": "", "derived_from": "",
         "updated_by": "", "created_at": "", "updated_at": ""},
    ]


def test_restoring_an_export_writes_uploaded_rows_and_recomputes_the_rest(
        seeded_engine):
    with Session(seeded_engine) as session:
        add_customers(session, ("CUST-A", "Dhiren & Brothers", "STR001"),
                      ("CUST-B", "Anis & Sons", "STR001"))
        report = geo.restore_locations(session, export_rows(), actor="reload")
        session.commit()

        assert report.restored == {"customer": 2}
        assert report.skipped_derived == {"sub_territory": 1}
        assert report.unknown == [("customer", "CUST-GONE"), ("warehouse", "WH1")]
        assert [(level, code) for level, code, _ in report.invalid] == [
            ("region", "REG002"),
        ]
        assert report.derived["sub_territory"] == 1

        customer = placed(session, "customer", "CUST-A")
        assert customer.source == GeoSource.UPLOAD
        assert customer.label == "Dhiren & Brothers"
        assert customer.updated_by == "reload"
        assert placed(session, "customer", "CUST-B").label is None

        # The derived row was recomputed from the restored customers, not
        # copied from the export's (1, 1).
        sub_territory = placed(session, "sub_territory", "STR001")
        assert sub_territory.source == GeoSource.DERIVED
        assert sub_territory.derived_from == 2
        assert 22 < sub_territory.latitude < 24
        assert placed(session, "customer", "CUST-GONE") is None
        assert placed(session, "region", "REG002") is None


def test_restoring_twice_moves_nothing_and_duplicates_nothing(seeded_engine):
    with Session(seeded_engine) as session:
        add_customers(session, ("CUST-A", "Dhiren & Brothers", "STR001"),
                      ("CUST-B", "Anis & Sons", "STR001"))
        geo.restore_locations(session, export_rows())
        geo.restore_locations(session, export_rows())
        session.commit()
        rows = session.execute(select(MapEntityLocation)).scalars().all()
        assert len({(row.entity_type, row.entity_code) for row in rows}) == len(rows)
        assert sum(row.entity_type == "customer" for row in rows) == 2


def test_the_reload_script_reports_before_it_writes(seeded_engine, tmp_path,
                                                    monkeypatch, capsys):
    """Dry by default, and the target is printed before anything happens."""
    import importlib.util
    import sys
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "reload_map_locations", scripts / "reload_map_locations.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    export = tmp_path / "map_entity_locations.csv"
    with export.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(export_rows()[0]))
        writer.writeheader()
        writer.writerows(export_rows())

    with Session(seeded_engine) as session:
        add_customers(session, ("CUST-A", "Dhiren & Brothers", "STR001"),
                      ("CUST-B", "Anis & Sons", "STR001"))
        session.commit()

    import app.database.connection as connection
    monkeypatch.setattr(connection, "get_engine", lambda *a, **k: seeded_engine)
    monkeypatch.setattr(module, "get_engine", lambda *a, **k: seeded_engine)

    assert module.main(["--file", str(export)]) == 0
    out = capsys.readouterr().out
    assert "Target:" in out and "Dry run" in out
    assert "customer" in out and "CUST-GONE" in out
    with Session(seeded_engine) as session:
        assert placed(session, "customer", "CUST-A") is None, "a dry run writes nothing"

    assert module.main(["--file", str(export), "--apply"]) == 0
    assert "Committed" in capsys.readouterr().out
    with Session(seeded_engine) as session:
        assert placed(session, "customer", "CUST-A") is not None
        assert placed(session, "zone", "Z001") is not None

    sys.modules.pop("reload_map_locations", None)


# ==========================================================================
# Coordinates through the Data Upload Center
# ==========================================================================


@pytest.fixture
def upload_client(agent_engine, users, monkeypatch):
    import app.database.connection as connection

    monkeypatch.setattr(connection, "get_engine", lambda *a, **k: agent_engine)

    with Session(agent_engine) as session:
        session.add(AppUser(username="root", display_name="Administrator",
                            role=Role.SUPER_ADMIN, is_active=True,
                            status=UserStatus.ACTIVE,
                            password_hash=hash_password(PASSWORD)))
        add_customers(session, ("CUST-A", "Dhiren & Brothers", "STR001"))
        session.commit()

    def _session_override():
        session = Session(bind=agent_engine, expire_on_commit=False, future=True)
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = _session_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def login(client: TestClient, username: str = "root") -> str:
    response = client.post("/api/auth/login",
                           json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def csv_bytes(rows: list[list[object]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["Entity Type", "Entity Code", "Latitude", "Longitude", "Label"])
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def upload_locations(client: TestClient, token: str, content: bytes):
    """Upload locations and wait for the validation job to finish.

    Uploading is a background job — the request answers 202 with a job to
    watch — so the shared helpers from the upload suite are reused here rather
    than this file growing its own copy of the wait.
    """
    from test_data_upload import FinishedUpload, _as_outcome, wait_for_job

    response = client.post("/api/data-upload/preview", headers=auth(token),
                           files={"file": ("locations.csv", content, "text/csv")},
                           data={"upload_type": "map_entity_locations",
                                 "import_mode": "UPSERT"})
    if response.status_code != 202:
        return response
    upload_id = response.json()["upload"]["upload_id"]
    return FinishedUpload(202, _as_outcome(wait_for_job(client, token, upload_id)))


def commit_locations(client: TestClient, token: str, upload_id: int):
    from test_data_upload import FinishedUpload, _as_outcome, wait_for_job

    response = client.post(f"/api/data-upload/{upload_id}/commit",
                           params={"confirm": True}, headers=auth(token))
    if response.status_code != 202:
        return response
    return FinishedUpload(202, _as_outcome(wait_for_job(client, token, upload_id)))


def test_the_upload_centre_offers_a_map_location_type(upload_client):
    token = login(upload_client)
    body = upload_client.get("/api/data-upload/types", headers=auth(token)).json()
    master = next(c for c in body["categories"] if c["key"] == "MASTER")
    location_type = next(t for t in master["types"]
                         if t["key"] == "map_entity_locations")
    assert location_type["business_key"] == ["entity_type", "entity_code"]
    assert "Latitude" in location_type["required_columns"]


def test_the_location_template_downloads(upload_client):
    token = login(upload_client)
    response = upload_client.get(
        "/api/data-upload/types/map_entity_locations/template",
        params={"format": "csv"}, headers=auth(token))
    assert response.status_code == 200
    header = response.content.decode("utf-8-sig").splitlines()[0]
    assert header.startswith("Entity Type,Entity Code,Latitude,Longitude")


def test_coordinates_upload_through_the_validated_pipeline(upload_client,
                                                            agent_engine):
    token = login(upload_client)
    content = csv_bytes([
        ["customer", "CUST-A", 23.7808, 90.4008, "Dhiren & Brothers"],
        ["region", "REG002", 22.8456, 89.5403, "Khulna"],
    ])
    preview = upload_locations(upload_client, token, content).json()
    assert preview["upload"]["status"] == "VALIDATED", preview
    assert preview["upload"]["totals"]["valid_rows"] == 2

    result = commit_locations(upload_client, token,
                              preview["upload"]["upload_id"]).json()
    assert result["upload"]["status"] == "COMPLETED", result
    assert result["upload"]["totals"]["inserted_rows"] == 2

    with Session(agent_engine) as session:
        assert placed(session, "customer", "CUST-A") is not None
        # The post-load hook derived the levels above the uploaded customer.
        area = placed(session, "area", "AR001")
        assert area is not None and area.source == GeoSource.DERIVED
        # The uploaded region is authoritative and untouched by derivation.
        region = placed(session, "region", "REG002")
        assert region.source == GeoSource.UPLOAD


def test_an_upload_with_bad_coordinates_reports_row_and_reason(upload_client):
    token = login(upload_client)
    content = csv_bytes([
        ["customer", "CUST-A", 23.78, 90.40, ""],          # fine
        ["customer", "CUST-A", 24.00, 91.00, ""],          # duplicate in file
        ["wormhole", "WH1", 23.0, 90.0, ""],               # unknown level
        ["territory", "TR-NOPE", 23.0, 90.0, ""],          # unknown code
        ["region", "REG001", 0, 0, ""],                    # null island
    ])
    body = upload_locations(upload_client, token, content).json()
    assert body["upload"]["totals"]["valid_rows"] == 1
    codes = {issue["error_code"] for issue in body["errors"]}
    assert "DUPLICATE_IN_FILE" in codes
    assert "INVALID_TYPE" in codes
    assert "INVALID_PARENT_CODE" in codes
    for issue in body["errors"]:
        assert issue["row"] and issue["suggested_fix"]


# ==========================================================================
# Coordinates in Data Management
# ==========================================================================


@pytest.fixture
def datamgmt_client(agent_engine, monkeypatch):
    """A signed-in administrator against the Data Management API."""
    import app.database.connection as connection

    monkeypatch.setattr(connection, "get_engine", lambda *a, **k: agent_engine)

    with Session(agent_engine) as session:
        session.add(AppUser(username="root", display_name="Administrator",
                            role=Role.SUPER_ADMIN, is_active=True,
                            status=UserStatus.ACTIVE,
                            password_hash=hash_password(PASSWORD)))
        session.commit()

    def _session_override():
        session = Session(bind=agent_engine, expire_on_commit=False, future=True)
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = _session_override
    try:
        client = TestClient(app)
        yield client, login(client)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def seeded(agent_engine):
    """Two customers, one of them placed, and the centroids that follow.

    Placing a customer and deriving is what produces a ``DERIVED``
    sub-territory, which is the row the provenance tests need: they are about
    what happens when somebody corrects a centroid by hand.
    """
    with Session(agent_engine) as session:
        add_customers(session,
                      ("CUST-A", "Dhiren & Brothers", "STR001"),
                      ("CUST-B", "Rahim Stores", "STR001"))
        geo.upsert_location(session, entity_type="customer", entity_code="CUST-A",
                            latitude=23.75, longitude=90.40)
        geo.derive_parents(session)
        session.commit()
#
# They were deliberately excluded from this screen until the Upload Centre
# turned out to be the *only* way to reach one: a coordinate could be loaded and
# then never seen, corrected or removed, and a bulk overwrite was the only edit
# available. The tests below hold the two things that exclusion was protecting —
# the derivation and the provenance — now that the rows are visible.


def test_coordinates_are_a_data_management_entity():
    """Listed under Market, keyed on the pair, and removed rather than retired."""
    from app.datamgmt.catalogue import MASTER_ENTITIES, get_master

    assert "map_entity_locations" in {entity.key for entity in MASTER_ENTITIES}

    entity = get_master("map_entity_locations")
    assert entity.to_dict()["group"] == "MARKET"
    assert entity.key_fields == ("entity_type", "entity_code")
    # No is_deleted column to set, so the row goes and the change log keeps it.
    assert entity.soft_delete is False
    # Every column of the table is worth seeing; longitude in particular falls
    # outside the generic "first three" promotion. ``source`` is not an upload
    # column at all — nobody uploads provenance — but it is what tells a placed
    # coordinate from a computed one, which is what decides whether the row can
    # be removed, so a reader cannot do without it.
    assert entity.to_dict()["default_columns"] == [
        "entity_type", "entity_code", "latitude", "longitude", "label", "source",
    ]
    assert entity.field_by_name["source"].editable is False
    assert entity.field_by_name["derived_from"].editable is False


def test_the_entity_type_choices_come_from_the_map_itself():
    """Offered values are exactly the levels the coordinate loader accepts."""
    from app.datamgmt.catalogue import get_master

    entity = get_master("map_entity_locations")
    assert set(entity.field_by_name["entity_type"].choices) == set(levels.LEVEL_BY_KEY)


def test_the_table_lists_every_placed_coordinate(datamgmt_client, seeded):
    client, token = datamgmt_client
    body = client.get("/api/master/map_entity_locations",
                      headers=auth(token)).json()

    rows = {(row["entity_type"], row["entity_code"]): row for row in body["rows"]}
    assert ("customer", "CUST-A") in rows
    assert rows[("customer", "CUST-A")]["latitude"] == pytest.approx(23.75)
    # The composite key is what addresses the row, not its first column.
    assert rows[("customer", "CUST-A")]["_key"] == "customer|CUST-A"


def test_one_coordinate_is_addressed_by_its_whole_key(datamgmt_client, seeded):
    """`customer` alone names 2 rows; only the pair names one."""
    client, token = datamgmt_client

    response = client.get("/api/master/map_entity_locations/customer%7CCUST-A",
                          headers=auth(token))
    assert response.status_code == 200, response.text
    assert response.json()["record"]["entity_code"] == "CUST-A"

    # The first key column on its own is not a record.
    assert client.get("/api/master/map_entity_locations/customer",
                      headers=auth(token)).status_code == 422


def test_editing_a_derived_centroid_makes_it_manual_and_it_then_survives(
        datamgmt_client, seeded, agent_engine):
    """The provenance rule the map owns, kept on the Data Management path.

    A centroid corrected by hand must stop being a centroid, or the next
    derivation silently recomputes it back and the reader has no way to see why
    their correction keeps disappearing.
    """
    client, token = datamgmt_client
    with Session(agent_engine) as session:
        assert placed(session, "sub_territory", "STR001").source == GeoSource.DERIVED

    response = client.put("/api/master/map_entity_locations/sub_territory%7CSTR001",
                          headers=auth(token),
                          json={"values": {"latitude": 24.5, "longitude": 91.5}})
    assert response.status_code == 200, response.text

    with Session(agent_engine) as session:
        row = placed(session, "sub_territory", "STR001")
        assert row.source == GeoSource.MANUAL
        assert row.derived_from is None
        assert row.latitude == pytest.approx(24.5)

    # And a later derivation leaves the hand-placed figure alone.
    with Session(agent_engine) as session:
        geo.derive_parents(session)
        session.commit()
    with Session(agent_engine) as session:
        assert placed(session, "sub_territory", "STR001").latitude == pytest.approx(24.5)


def test_moving_a_customer_re_derives_the_levels_above_it(datamgmt_client, seeded,
                                                          agent_engine):
    """The other half of what the exclusion protected: parents follow children."""
    client, token = datamgmt_client
    with Session(agent_engine) as session:
        before = placed(session, "sub_territory", "STR001").latitude

    response = client.put("/api/master/map_entity_locations/customer%7CCUST-A",
                          headers=auth(token),
                          json={"values": {"latitude": 25.0, "longitude": 91.0}})
    assert response.status_code == 200, response.text

    with Session(agent_engine) as session:
        after = placed(session, "sub_territory", "STR001")
        assert after.source == GeoSource.DERIVED
        assert after.latitude != pytest.approx(before)


def test_removing_a_coordinate_takes_the_row_and_keeps_the_record(
        datamgmt_client, seeded, agent_engine):
    client, token = datamgmt_client
    response = client.request(
        "DELETE", "/api/master/map_entity_locations/customer%7CCUST-A",
        headers=auth(token), json={"reason": "placed on the wrong side of the river"})
    assert response.status_code == 200, response.text
    assert "removed" in response.json()["message"]

    with Session(agent_engine) as session:
        assert placed(session, "customer", "CUST-A") is None
        # The whole record survives in the change log, which is what makes a
        # physical delete acceptable for this one table.
        from app.database.models_admin import DataChangeLog

        entry = session.execute(
            select(DataChangeLog).where(
                DataChangeLog.record_key == "customer|CUST-A")
        ).scalars().first()
        assert entry is not None


def test_removing_a_derived_centroid_is_refused_rather_than_undone(
        datamgmt_client, seeded, agent_engine):
    """Regression: the delete used to succeed and the derivation put it back.

    ``_after_location_write`` re-derives after every write, and
    ``_write_centroids`` writes a centroid for every parent whose children are
    still placed — so removing a derived row committed the delete and then
    recreated an identical row in the same transaction. The caller was told the
    coordinate was gone while looking at it, which is the worst of the three
    possible outcomes.
    """
    client, token = datamgmt_client
    with Session(agent_engine) as session:
        assert placed(session, "sub_territory", "STR001").source == GeoSource.DERIVED

    response = client.request(
        "DELETE", "/api/master/map_entity_locations/sub_territory%7CSTR001",
        headers=auth(token), json={})
    assert response.status_code == 409, response.text
    detail = str(response.json()["detail"])
    assert "centroid" in detail
    # The refusal has to say what *does* remove it, or it is a dead end.
    assert "below it" in detail

    with Session(agent_engine) as session:
        assert placed(session, "sub_territory", "STR001") is not None


def test_the_table_says_which_rows_can_be_removed(datamgmt_client, seeded):
    """So the screen can leave the control off a row that would only refuse."""
    client, token = datamgmt_client
    body = client.get("/api/master/map_entity_locations",
                      headers=auth(token)).json()

    by_key = {row["_key"]: row for row in body["rows"]}
    assert by_key["customer|CUST-A"]["_removable"] is True
    assert by_key["customer|CUST-A"]["source"] == GeoSource.UPLOAD
    assert by_key["sub_territory|STR001"]["_removable"] is False
    assert by_key["sub_territory|STR001"]["source"] == GeoSource.DERIVED


def test_a_derived_centroid_goes_when_the_coordinates_below_it_do(
        datamgmt_client, seeded, agent_engine):
    """The other half, and what makes the refusal's advice true.

    A centroid whose inputs have all been removed is a position nothing
    supports — the same wrongness ``_prune_orphaned_centroids`` catches for an
    entity the master dropped, reached by a different route. Without this, the
    advice "remove the coordinates below it" led nowhere: the parent kept a
    centroid derived from a point that no longer existed, and being derived it
    could not be removed either.
    """
    client, token = datamgmt_client
    with Session(agent_engine) as session:
        assert placed(session, "sub_territory", "STR001") is not None

    # CUST-A is the only placed coordinate under STR001.
    response = client.request(
        "DELETE", "/api/master/map_entity_locations/customer%7CCUST-A",
        headers=auth(token), json={})
    assert response.status_code == 200, response.text

    with Session(agent_engine) as session:
        assert placed(session, "customer", "CUST-A") is None
        assert placed(session, "sub_territory", "STR001") is None
        # And every level the chain carried up from it.
        assert placed(session, "territory", "TR001") is None


def test_removing_a_placed_parent_says_it_falls_back_to_a_centroid(
        datamgmt_client, seeded, agent_engine):
    """Succeeding and leaving a row behind is not a failure, but it looks like one.

    An entity with coordinates below it is never unplaced by removing its own:
    the derivation immediately gives it their centroid, which is the right
    answer and indistinguishable on screen from the delete not working. So the
    message says what happened and what would take it off the map.
    """
    client, token = datamgmt_client
    # Place the sub-territory by hand, which makes it the caller's to remove.
    edit = client.put("/api/master/map_entity_locations/sub_territory%7CSTR001",
                      headers=auth(token),
                      json={"values": {"latitude": 24.1, "longitude": 90.9}})
    assert edit.status_code == 200, edit.text
    with Session(agent_engine) as session:
        assert placed(session, "sub_territory", "STR001").source == GeoSource.MANUAL

    response = client.request(
        "DELETE", "/api/master/map_entity_locations/sub_territory%7CSTR001",
        headers=auth(token), json={})
    assert response.status_code == 200, response.text
    message = response.json()["message"]
    assert "still on the map" in message
    assert "falls back to the centroid" in message

    with Session(agent_engine) as session:
        # The hand-placed coordinate is genuinely gone; what stands is derived.
        assert placed(session, "sub_territory", "STR001").source == GeoSource.DERIVED


def test_a_removed_coordinate_cannot_be_restored(datamgmt_client, seeded):
    """There is nothing to un-retire; the honest answer is to create it again."""
    client, token = datamgmt_client
    client.request("DELETE", "/api/master/map_entity_locations/customer%7CCUST-A",
                   headers=auth(token), json={})
    response = client.post(
        "/api/master/map_entity_locations/customer%7CCUST-A/restore",
        headers=auth(token))
    assert response.status_code == 409
    assert "create the record again" in response.json()["detail"].lower()


@pytest.mark.parametrize("values,expected", [
    ({"entity_type": "wormhole", "entity_code": "X1",
      "latitude": 23.0, "longitude": 90.0}, "not a map level"),
    ({"entity_type": "territory", "entity_code": "TR-NOPE",
      "latitude": 23.0, "longitude": 90.0}, "does not exist in the master data"),
    ({"entity_type": "territory", "entity_code": "TR001",
      "latitude": 0, "longitude": 0}, "Atlantic"),
])
def test_the_form_is_held_to_the_same_rules_as_a_file(datamgmt_client, seeded,
                                                      values, expected):
    """One definition of the coordinate rules, reached from both entry points."""
    client, token = datamgmt_client
    response = client.post("/api/master/map_entity_locations", headers=auth(token),
                           json={"values": values})
    assert response.status_code == 422, response.text
    assert expected in str(response.json()["detail"])


def test_a_second_coordinate_for_one_entity_is_refused_on_the_pair(
        datamgmt_client, seeded):
    """The duplicate check reads the whole key, not just the entity type.

    Reading the first column alone would refuse the *second* territory anybody
    placed, which is the ordinary case rather than the error.
    """
    client, token = datamgmt_client

    # A different territory is fine even though the entity type is taken.
    ok = client.post("/api/master/map_entity_locations", headers=auth(token),
                     json={"values": {"entity_type": "customer",
                                      "entity_code": "CUST-B",
                                      "latitude": 23.9, "longitude": 90.1}})
    assert ok.status_code in (200, 201), ok.text

    # The same pair twice is not.
    clash = client.post("/api/master/map_entity_locations", headers=auth(token),
                        json={"values": {"entity_type": "customer",
                                         "entity_code": "CUST-B",
                                         "latitude": 24.0, "longitude": 90.2}})
    assert clash.status_code == 422
    assert "already exists" in str(clash.json()["detail"])
