"""End-to-end tests for the shared-node correction API.

Covers: batch correction of every served relation referencing a node,
incrementing revisions, idempotent re-submission, raw-snapshot/history
preservation, all-or-nothing failures (no match, >8 cap, rebuild
failure), interleaving with ordinary uploads, restart durability,
atomic visibility under concurrency and /docs exposure.
"""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from app.database import Database
from app.main import app

from .factories import SQUARE_NODES, SQUARE_WAY, node, revision, way


def post_revision(client: TestClient, relation_id: int, payload: dict):
    return client.post(f"/relations/{relation_id}/revisions", json=payload)


def correct(client: TestClient, node_id: int, lat: float, lon: float):
    return client.post(
        f"/nodes/{node_id}/corrections", json={"lat": lat, "lon": lon}
    )


# ---------------------------------------------------------------- test fixtures
def polygon_using_shared_node(rev: int = 1) -> dict:
    """A second valid square sharing node 1 (id and position (0,0)).

    Nodes 11..13 are unique to this relation; node 1 is the shared OSM
    node whose correction must propagate to every relation using it.
    """
    nodes = [
        node(1, 0.0, 0.0),
        node(11, 2.0, 0.0),
        node(12, 2.0, 2.0),
        node(13, 0.0, 2.0),
    ]
    return revision(rev, nodes, [way(30, [1, 11, 12, 13, 1])])


def inner_ring_violation_revision(rev: int = 1) -> dict:
    """A valid big square (sharing node 1) with its own inner ring.

    Moving shared node 1 onto / inside the inner ring deterministically
    breaks the rebuild with ``inner_ring_violation``, while an ordinary
    unit square survives the same move.
    """
    outer = [
        node(1, 0.0, 0.0),
        node(31, 3.0, 0.0),
        node(32, 3.0, 3.0),
        node(33, 0.0, 3.0),
    ]
    inner = [
        node(34, 1.0, 1.0),
        node(35, 2.0, 1.0),
        node(36, 2.0, 2.0),
        node(37, 1.0, 2.0),
    ]
    return revision(
        rev,
        outer + inner,
        [way(60, [1, 31, 32, 33, 1]), way(61, [34, 35, 36, 37, 34], role="inner")],
    )


def seed_two_served_relations(client: TestClient, rel_a: int, rel_b: int) -> None:
    assert post_revision(client, rel_a, revision(1, SQUARE_NODES, [SQUARE_WAY])).status_code == 200
    assert post_revision(client, rel_b, polygon_using_shared_node()).status_code == 200


# -------------------------------------------------------------------- happy path
def test_correction_creates_one_new_revision_per_served_relation(
    client: TestClient, relation_id: int
):
    rel_a, rel_b, rel_c = relation_id, relation_id + 1, relation_id + 2
    seed_two_served_relations(client, rel_a, rel_b)
    # rel_c does not reference node 1 -> must not appear in the batch.
    other_nodes = [node(50, 5.0, 5.0), node(51, 6.0, 5.0),
                   node(52, 6.0, 6.0), node(53, 5.0, 6.0)]
    post_revision(client, rel_c, revision(1, other_nodes, [way(50, [50, 51, 52, 53, 50])]))

    resp = correct(client, 1, lat=0.25, lon=0.1)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["node_id"] == 1
    assert body["lat"] == 0.25 and body["lon"] == 0.1

    items = {item["relation_id"]: item for item in body["corrections"]}
    assert sorted(items) == [rel_a, rel_b]
    for item in items.values():
        assert item["changed"] is True
        assert item["source_revision"] == 1
        assert item["revision"] == 2
        assert item["geometry"]["type"] == "Polygon"

    # Both /current faces now serve revision 2 with the corrected node.
    for rel in (rel_a, rel_b):
        cur = client.get(f"/relations/{rel}/current").json()
        assert cur["revision"] == 2
        assert cur["status"] == "valid"
        stored = client.get(f"/relations/{rel}/revisions/2").json()
        assert stored["input"]["nodes"][0]["lat"] == 0.25
        assert stored["input"]["nodes"][0]["lon"] == 0.1

    # The untouched relation stays on revision 1.
    assert client.get(f"/relations/{rel_c}/current").json()["revision"] == 1


def test_same_coordinates_resubmitted_creates_no_new_revision(
    client: TestClient, relation_id: int
):
    rel_a, rel_b = relation_id, relation_id + 1
    seed_two_served_relations(client, rel_a, rel_b)

    first = correct(client, 1, lat=0.25, lon=0.1).json()
    assert all(item["changed"] is True for item in first["corrections"])

    # Identical coordinates: idempotent, no revision is minted anywhere.
    again = correct(client, 1, lat=0.25, lon=0.1)
    assert again.status_code == 200
    for item in again.json()["corrections"]:
        assert item["changed"] is False
        assert item["source_revision"] == item["revision"] == 2

    for rel in (rel_a, rel_b):
        listing = client.get(f"/relations/{rel}/revisions").json()["revisions"]
        assert [r["revision"] for r in listing] == [1, 2]


def test_mixed_changed_and_unchanged_relations_in_one_batch(
    client: TestClient, relation_id: int
):
    rel_a, rel_b = relation_id, relation_id + 1
    seed_two_served_relations(client, rel_a, rel_b)
    # Advance rel_b so node 1 there already sits at the target coordinates.
    moved = polygon_using_shared_node(rev=2)
    moved["nodes"][0] = node(1, 0.1, 0.25)  # node(id, lon, lat) -> lon=0.1, lat=0.25
    assert post_revision(client, rel_b, moved).status_code == 200

    resp = correct(client, 1, lat=0.25, lon=0.1).json()
    items = {item["relation_id"]: item for item in resp["corrections"]}
    assert items[rel_a]["changed"] is True
    assert items[rel_a]["revision"] == 2
    assert items[rel_b]["changed"] is False
    assert items[rel_b]["revision"] == 2
    # The unchanged relation still gets its current geometry echoed back.
    assert items[rel_b]["geometry"]["type"] == "Polygon"
    assert client.get(f"/relations/{rel_b}/revisions").json()["revisions"] == [
        {"revision": 1, "status": "valid"},
        {"revision": 2, "status": "valid"},
    ]


def test_correction_revision_numbers_continue_after_existing_history(
    client: TestClient, relation_id: int
):
    rel = relation_id
    post_revision(client, rel, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    post_revision(client, rel, revision(2, SQUARE_NODES, [SQUARE_WAY]))

    body = correct(client, 1, 0.3, 0.4).json()
    item = body["corrections"][0]
    assert item["source_revision"] == 2
    assert item["revision"] == 3
    # An ordinary upload afterwards must take the next number, as before.
    conflict = post_revision(client, rel, revision(3, SQUARE_NODES, [SQUARE_WAY]))
    assert conflict.status_code == 409
    ok = post_revision(client, rel, revision(4, SQUARE_NODES, [SQUARE_WAY]))
    assert ok.status_code == 200
    assert [r["revision"] for r in client.get(f"/relations/{rel}/revisions").json()["revisions"]] == [1, 2, 3, 4]


def test_historical_revisions_and_raw_snapshots_remain_readable(
    client: TestClient, relation_id: int
):
    rel = relation_id
    post_revision(client, rel, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    correct(client, 1, 0.25, 0.1)

    old = client.get(f"/relations/{rel}/revisions/1").json()
    assert old["status"] == "valid"
    assert old["input"]["nodes"][0] == {"id": 1, "lat": 0.0, "lon": 0.0}

    new = client.get(f"/relations/{rel}/revisions/2").json()
    assert new["input"]["revision"] == 2
    assert new["input"]["nodes"][0] == {"id": 1, "lat": 0.25, "lon": 0.1}
    # Ways and the other nodes are copied verbatim from the raw snapshot.
    assert new["input"]["ways"] == [SQUARE_WAY]
    assert new["geometry"]["type"] == "Polygon"


# ------------------------------------------------------------------ failure cases
def test_no_matching_relation_is_a_404_and_changes_nothing(
    client: TestClient, relation_id: int
):
    rel = relation_id
    post_revision(client, rel, revision(1, SQUARE_NODES, [SQUARE_WAY]))

    resp = correct(client, 999, lat=1.0, lon=1.0)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "no_matching_relations"
    assert client.get(f"/relations/{rel}/current").json()["revision"] == 1


def test_relation_whose_latest_revision_is_invalid_is_not_corrected(
    client: TestClient, relation_id: int
):
    rel = relation_id
    post_revision(client, rel, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    # Invalid revision stops serving the relation: node 1 has no *current* face.
    post_revision(client, rel, revision(2, SQUARE_NODES, [way(10, [1, 2, 3, 99, 1])]))
    assert client.get(f"/relations/{rel}/current").status_code == 409

    resp = correct(client, 1, lat=0.9, lon=0.9)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "no_matching_relations"
    # Nothing was written; history and state machine are untouched.
    assert [r["revision"] for r in client.get(f"/relations/{rel}/revisions").json()["revisions"]] == [1, 2]
    assert client.get(f"/relations/{rel}/current").status_code == 409


def test_more_than_eight_relations_rejects_the_whole_batch(
    client: TestClient, relation_id: int
):
    rels = [relation_id + i for i in range(9)]
    for rel in rels:
        # Each relation is a valid served face referencing shared node 1.
        post_revision(client, rel, revision(1, SQUARE_NODES, [SQUARE_WAY]))

    resp = correct(client, 1, lat=0.25, lon=0.1)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "correction_limit_exceeded"
    assert detail["matching_relations"] == 9
    assert detail["limit"] == 8

    # All nine relations remain exactly on revision 1.
    for rel in rels:
        assert client.get(f"/relations/{rel}/current").json()["revision"] == 1


def test_rebuild_failure_rolls_back_every_relation_in_the_batch(
    client: TestClient, relation_id: int
):
    rel_a, rel_b = relation_id, relation_id + 1
    post_revision(client, rel_a, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    post_revision(client, rel_b, inner_ring_violation_revision())

    # The move keeps rel_a's square valid but pushes node 1 inside rel_b's
    # inner ring, so that rebuild fails with inner_ring_violation.
    resp = correct(client, 1, lat=1.5, lon=1.5)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "rebuild_failed"
    assert detail["relation_id"] == rel_b
    assert detail["geometry_error"]["code"] == "inner_ring_violation"
    assert detail["geometry_error"]["way_ids"] == [61]

    # All-or-nothing: neither relation got a new revision.
    for rel in (rel_a, rel_b):
        cur = client.get(f"/relations/{rel}/current").json()
        assert cur["revision"] == 1
        stored = client.get(f"/relations/{rel}/revisions/1").json()
        assert stored["input"]["nodes"][0] == {"id": 1, "lat": 0.0, "lon": 0.0}
        assert [r["revision"] for r in client.get(f"/relations/{rel}/revisions").json()["revisions"]] == [1]


def test_invalid_coordinates_are_422_and_never_reach_storage(
    client: TestClient, relation_id: int
):
    rel = relation_id
    post_revision(client, rel, revision(1, SQUARE_NODES, [SQUARE_WAY]))
    assert correct(client, 1, lat=91.0, lon=0.0).status_code == 422
    assert correct(client, 1, lat=0.0, lon=-181.0).status_code == 422
    assert client.get(f"/relations/{rel}/current").json()["revision"] == 1


# --------------------------------------------------------------- durability & ACID
def test_corrections_survive_database_restart(
    client: TestClient, db_path: str, relation_id: int
):
    rel_a, rel_b = relation_id, relation_id + 1
    seed_two_served_relations(client, rel_a, rel_b)
    correct(client, 1, 0.25, 0.1)

    restarted = Database(db_path)
    app.state.db = restarted

    for rel in (rel_a, rel_b):
        row = restarted.get_revision(rel, 2)
        assert row is not None and row["status"] == "valid"
        assert row["input"]["nodes"][0]["lat"] == 0.25
    listing = restarted.find_current_relations_referencing_node
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = listing(conn, 1)
        assert sorted(r["relation_id"] for r in rows) == [rel_a, rel_b]
        assert all(r["revision"] == 2 for r in rows)
    finally:
        conn.close()


def test_interleaved_upload_serialises_against_correction_and_keeps_atomic_visibility(
    client: TestClient, db: Database, relation_id: int
):
    rel_a, rel_b = relation_id, relation_id + 1
    seed_two_served_relations(client, rel_a, rel_b)
    barrier = threading.Event()

    from app.geometry import build_polygon_geojson
    from app.models import RevisionIn

    def blocking_rebuild(payload: dict) -> dict:
        # Hold the write transaction open so an ordinary upload must queue
        # behind it; in the meantime readers only see the old committed state.
        barrier.wait(timeout=10)
        return build_polygon_geojson(RevisionIn(**payload))

    def run_correction() -> Exception | None:
        try:
            db.correct_node(1, 0.25, 0.1, blocking_rebuild)
        except Exception as exc:  # pragma: no cover - surfaced via assertion
            return exc
        return None

    t = threading.Thread(target=run_correction)
    t.start()
    # Give the thread time to acquire BEGIN IMMEDIATE.
    assert barrier.wait(timeout=5) is False

    # While the correction transaction is open, readers must not see any
    # partial batch: both relations still serve revision 1.
    for rel in (rel_a, rel_b):
        assert client.get(f"/relations/{rel}/current").json()["revision"] == 1

    # An interleaved upload blocks on the write lock, then commits after
    # the correction; it must not be able to commit in the middle of it.
    # A separate TestClient gives the worker thread its own anyio portal.
    upload_client = TestClient(app)
    upload_client.__enter__()
    app.state.db = db
    upload_result: dict = {}

    def upload() -> None:
        resp = upload_client.post(
            f"/relations/{rel_a}/revisions",
            json=revision(3, SQUARE_NODES, [SQUARE_WAY]),
        )
        upload_result["status"] = resp.status_code

    ut = threading.Thread(target=upload)
    ut.start()
    barrier.set()
    t.join(timeout=10)
    ut.join(timeout=10)
    upload_client.__exit__(None, None, None)
    assert not t.is_alive() and not ut.is_alive()

    # Correction committed first (revision 2 for both relations); the
    # queued upload then took revision 3 on rel_a without conflict.
    assert upload_result["status"] == 200
    assert client.get(f"/relations/{rel_a}/current").json()["revision"] == 3
    assert client.get(f"/relations/{rel_b}/current").json()["revision"] == 2
    revs_a = [r["revision"] for r in client.get(f"/relations/{rel_a}/revisions").json()["revisions"]]
    assert revs_a == [1, 2, 3]
    # The corrected geometry was really rebuilt, not copied.
    cur_b = client.get(f"/relations/{rel_b}/revisions/2").json()
    assert cur_b["input"]["nodes"][0] == {"id": 1, "lat": 0.25, "lon": 0.1}


def test_concurrent_identical_corrections_are_idempotent(
    client: TestClient, relation_id: int
):
    rel_a, rel_b = relation_id, relation_id + 1
    seed_two_served_relations(client, rel_a, rel_b)

    responses: list[dict] = []
    errors: list[Exception] = []

    def call() -> None:
        try:
            r = correct(client, 1, 0.25, 0.1)
            assert r.status_code == 200, r.text
            responses.append(r.json())
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(responses) == 4
    for rel in (rel_a, rel_b):
        assert [r["revision"] for r in client.get(f"/relations/{rel}/revisions").json()["revisions"]] == [1, 2]
    # The first call mints revisions; the later ones all report unchanged.
    changed_flags = [
        item["changed"]
        for body in responses
        for item in body["corrections"]
    ]
    assert changed_flags.count(True) == 2
    assert changed_flags.count(False) == 6


def test_docs_expose_correction_endpoint(client: TestClient):
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/nodes/{node_id}/corrections" in schema["paths"]
