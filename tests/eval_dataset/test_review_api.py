from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from bidmate_rag.eval_dataset.review.api import create_review_app
from bidmate_rag.eval_dataset.review.repository import ReviewRepository


def _client(
    tmp_path: Path,
    schema_v2_fixture: Path,
) -> tuple[TestClient, str, dict[str, str]]:
    repository = ReviewRepository(
        tmp_path / "review.sqlite3",
        package_root=schema_v2_fixture.parent,
        export_root=tmp_path / "exports",
    )
    client = TestClient(create_review_app(repository))
    session = client.post("/api/session")
    csrf = session.cookies.get("bidmate_review_csrf")
    assert session.status_code == 201 and csrf
    headers = {"X-CSRF-Token": csrf}
    package_id = client.get("/api/packages").json()[0]["package_id"]
    imported = client.post(f"/api/packages/{package_id}/import", headers=headers)
    assert imported.status_code == 200
    return client, imported.json()["dataset_id"], headers


def test_valid_local_session_is_reused_instead_of_rotated(tmp_path: Path) -> None:
    repository = ReviewRepository(tmp_path / "review.sqlite3")
    client = TestClient(create_review_app(repository))

    first = client.post("/api/session")
    first_session = first.json()["session_id"]
    first_csrf = client.cookies.get("bidmate_review_csrf")
    second = client.post("/api/session")

    assert second.status_code == 201
    assert second.json()["session_id"] == first_session
    assert client.cookies.get("bidmate_review_csrf") == first_csrf
    assert repository.database.connection.execute(
        "SELECT COUNT(*) FROM review_sessions"
    ).fetchone()[0] == 1


def test_concurrent_local_reads_and_session_validation_share_sqlite_safely(
    tmp_path: Path,
    schema_v2_fixture: Path,
) -> None:
    repository = ReviewRepository(
        tmp_path / "review.sqlite3",
        package_root=schema_v2_fixture.parent,
        export_root=tmp_path / "exports",
    )
    package_id = repository.discover_packages()[0]["package_id"]
    session = repository.sessions.create()
    dataset_id = repository.import_discovered(
        package_id,
        actor_session_id=session.session_id,
    )["dataset_id"]

    def read_workspace() -> None:
        for _ in range(100):
            assert repository.get_resume(dataset_id)["dataset_id"] == dataset_id
            assert repository.query_items(dataset_id)["total"] > 0

    def validate_session() -> None:
        for _ in range(100):
            validated = repository.sessions.validate(
                session.session_token,
                session.csrf_token,
            )
            assert validated.session_id == session.session_id

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [
            executor.submit(read_workspace),
            executor.submit(read_workspace),
            executor.submit(read_workspace),
            executor.submit(validate_session),
            executor.submit(validate_session),
            executor.submit(validate_session),
        ]
        for future in futures:
            future.result()


def test_import_edit_conflict_approve_fork_and_history_via_loopback_api(
    tmp_path: Path,
    schema_v2_fixture: Path,
) -> None:
    client, dataset_id, headers = _client(tmp_path, schema_v2_fixture)
    item = client.get(f"/api/datasets/{dataset_id}/items").json()["items"][0]
    saved = client.put(
        f"/api/items/{item['item_id']}/draft",
        headers=headers,
        json={"base_revision": 1, "patch": {"question": "Edited through API?"}},
    )
    assert saved.status_code == 200
    stale = client.put(
        f"/api/items/{item['item_id']}/draft",
        headers=headers,
        json={"base_revision": 1, "patch": {"question": "stale"}},
    )
    assert stale.status_code == 409

    approved = client.post(
        f"/api/items/{item['item_id']}/approve",
        headers=headers,
        json={"base_revision": 2},
    )
    assert approved.status_code == 200
    history = client.get(f"/api/items/{item['item_id']}/snapshots")
    assert history.status_code == 200
    assert history.json()[0]["action"] == "approved"
    audit = client.get(f"/api/items/{item['item_id']}/audit")
    assert [event["event_type"] for event in audit.json()][:2] == ["approved", "draft_saved"]

    forked = client.post(
        f"/api/items/{item['item_id']}/fork",
        headers=headers,
        json={"base_revision": 3},
    )
    assert forked.status_code == 200
    rejected = client.post(
        f"/api/items/{forked.json()['item_id']}/reject",
        headers=headers,
        json={"base_revision": 1, "reason": "duplicate"},
    )
    assert rejected.status_code == 200


def test_manual_anchor_resolution_does_not_accept_client_hash(
    tmp_path: Path,
    schema_v2_fixture: Path,
) -> None:
    client, dataset_id, headers = _client(tmp_path, schema_v2_fixture)
    item = client.get(f"/api/datasets/{dataset_id}/items").json()["items"][0]
    item = client.get(f"/api/items/{item['item_id']}").json()
    anchor_id = item["evidence_anchors"][0]["anchor_id"]
    bbox = {
        "x0": 0.1,
        "y0": 0.1,
        "x1": 0.2,
        "y1": 0.2,
        "coordinate_space": "normalized_top_left",
        "page_width": 612,
        "page_height": 792,
        "rotation": 0,
    }
    wrong_quote = client.post(
        f"/api/items/{item['item_id']}/anchors/{anchor_id}/resolve",
        headers=headers,
        json={
            "base_revision": 1,
            "method": "manual",
            "selected_quote": "not the anchored quote",
            "page_number": item["evidence_anchors"][0]["pdf_page_number"],
            "bbox": bbox,
        },
    )
    assert wrong_quote.status_code == 422
    wrong_page = client.post(
        f"/api/items/{item['item_id']}/anchors/{anchor_id}/resolve",
        headers=headers,
        json={
            "base_revision": 1,
            "method": "manual",
            "selected_quote": item["evidence_anchors"][0]["exact_quote"],
            "page_number": item["evidence_anchors"][0]["pdf_page_number"] + 1,
            "bbox": bbox,
        },
    )
    assert wrong_page.status_code == 422
    response = client.post(
        f"/api/items/{item['item_id']}/anchors/{anchor_id}/resolve",
        headers=headers,
        json={
            "base_revision": 1,
            "method": "manual",
            "selected_quote": item["evidence_anchors"][0]["exact_quote"],
            "page_number": item["evidence_anchors"][0]["pdf_page_number"],
            "bbox": bbox,
        },
    )
    assert response.status_code == 200
    assert response.json()["evidence_anchors"][0]["resolution_method"] == "manual"


def test_export_uses_configured_root_and_exposes_checksum_status(
    tmp_path: Path,
    schema_v2_fixture: Path,
) -> None:
    client, dataset_id, headers = _client(tmp_path, schema_v2_fixture)
    item = client.get(f"/api/datasets/{dataset_id}/items").json()["items"][0]
    approved = client.post(
        f"/api/items/{item['item_id']}/approve",
        headers=headers,
        json={"base_revision": 1},
    )
    assert approved.status_code == 200

    exported = client.post(f"/api/datasets/{dataset_id}/exports", headers=headers)
    assert exported.status_code == 201
    body = exported.json()
    assert body["kind"] == "legacy_v1"
    assert body["item_count"] == 1
    assert len(body["checksum"]) == 64
    assert not Path(body["relative_path"]).is_absolute()
    assert "destination" not in body

    status = client.get(f"/api/exports/{body['export_id']}")
    assert status.status_code == 200
    assert status.json()["checksum"] == body["checksum"]
    assert (
        client.post(
            f"/api/datasets/{dataset_id}/exports/legacy",
            headers=headers,
            json={"destination": str(tmp_path / "client-selected")},
        ).status_code
        == 404
    )
