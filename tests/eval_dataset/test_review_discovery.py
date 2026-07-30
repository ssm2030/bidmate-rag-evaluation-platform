from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from bidmate_rag.eval_dataset.contract.models import Document, EvalItem, EvidenceAnchor
from bidmate_rag.eval_dataset.contract.package_io import write_package
from bidmate_rag.eval_dataset.review.api import create_review_app
from bidmate_rag.eval_dataset.review.repository import ReviewRepository


def _write_candidate(
    package_root: Path, pdf_root: Path, *, name: str = "candidate-package"
) -> Path:
    pdf_root.mkdir(parents=True, exist_ok=True)
    pdf = pdf_root / "safe.pdf"
    pdf.write_bytes(b"%PDF-1.4\nlocal review evidence\n%%EOF\n")
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    document = Document(
        document_id=uuid4(),
        relative_pdf_path=pdf.name,
        sha256=digest,
        page_count=1,
        legacy_filename=pdf.name,
        external_ids={"batch_id": "1"},
        source_classification="public",
        external_transmission_allowed=False,
    )
    anchor = EvidenceAnchor(
        anchor_id=uuid4(),
        ordinal=0,
        document_id=document.document_id,
        pdf_page_number=1,
        printed_page_label=None,
        exact_quote="Local review evidence",
        context_before=None,
        context_after=None,
        role="support",
        required=True,
        resolution_status="resolved",
        resolution_method="exact",
        document_sha256=digest,
        resolver_version="test-v1",
        bbox=None,
    )
    item = EvalItem(
        item_id=uuid4(),
        revision=1,
        status="needs_review",
        question="What evidence is present?",
        ground_truth_answer="Local review evidence",
        task_kind="extract",
        document_scope="single",
        answerability="answerable",
        evidence_mode="direct_quote",
        perturbation="none",
        difficulty="low",
        metadata_filter={"batch_id": 1},
        history=[],
        verification_notes=["review"],
        provenance={"sop_type": "A"},
        evidence_anchors=[anchor],
    )
    package = package_root / name
    write_package(package, dataset_id=uuid4(), documents=[document], items=[item])
    return package


def _session(client: TestClient) -> dict[str, str]:
    response = client.post("/api/session")
    assert response.status_code == 201
    assert response.cookies.get("bidmate_review_session")
    csrf = response.cookies.get("bidmate_review_csrf")
    assert csrf
    return {"X-CSRF-Token": csrf}


def test_dashboard_discovers_valid_and_invalid_packages_without_path_input(tmp_path: Path) -> None:
    package_root = tmp_path / "packages"
    package_root.mkdir()
    pdf_root = tmp_path / "pdf"
    valid = _write_candidate(package_root, pdf_root)
    invalid = package_root / "broken-package"
    invalid.mkdir()
    (invalid / "manifest.json").write_text("{}", encoding="utf-8")
    repository = ReviewRepository(
        tmp_path / "review.sqlite3",
        package_root=package_root,
        pdf_root=pdf_root,
    )
    client = TestClient(create_review_app(repository))

    candidates = client.get("/api/packages")
    assert candidates.status_code == 200
    cards = {entry["name"]: entry for entry in candidates.json()}
    assert cards[valid.name]["status"] == "valid"
    assert cards[valid.name]["item_count"] == 1
    assert cards[valid.name]["anchor_count"] == 1
    assert cards[valid.name]["schema_status"] == "pass"
    assert cards[valid.name]["checksum_status"] == "pass"
    assert cards[valid.name]["pdf_hash_status"] == "pass"
    assert "package_path" not in cards[valid.name]
    assert cards[invalid.name]["status"] == "invalid"
    assert cards[invalid.name]["blocking_reason"]
    assert not any(route.path == "/api/packages/import" for route in client.app.routes)


def test_identifier_import_summary_filters_and_resume_require_session_csrf(tmp_path: Path) -> None:
    package_root = tmp_path / "packages"
    package_root.mkdir()
    pdf_root = tmp_path / "pdf"
    _write_candidate(package_root, pdf_root)
    repository = ReviewRepository(
        tmp_path / "review.sqlite3",
        package_root=package_root,
        pdf_root=pdf_root,
    )
    client = TestClient(create_review_app(repository))
    package_id = client.get("/api/packages").json()[0]["package_id"]

    assert client.post(f"/api/packages/{package_id}/import").status_code == 401
    headers = _session(client)
    assert (
        client.post(
            f"/api/packages/{package_id}/import", headers={"X-CSRF-Token": "wrong"}
        ).status_code
        == 403
    )
    imported = client.post(f"/api/packages/{package_id}/import", headers=headers)
    assert imported.status_code == 200
    dataset_id = imported.json()["dataset_id"]

    summaries = client.get("/api/datasets").json()
    assert summaries[0]["counts"] == {
        "total": 1,
        "approved": 0,
        "needs_review": 1,
        "needs_anchor_fix": 0,
        "draft": 0,
        "rejected": 0,
    }
    assert summaries[0]["progress_percent"] == 0
    page = client.get(
        f"/api/datasets/{dataset_id}/items",
        params={
            "status": "needs_review",
            "sop_type": "A",
            "difficulty": "low",
            "page": 1,
            "page_size": 10,
        },
    ).json()
    assert page["total"] == 1
    assert page["items"][0]["anchor_count"] == 1
    item_id = page["items"][0]["item_id"]
    anchor_id = client.get(f"/api/items/{item_id}").json()["evidence_anchors"][0]["anchor_id"]

    saved = client.put(
        f"/api/datasets/{dataset_id}/resume",
        headers=headers,
        json={"item_id": item_id, "anchor_id": anchor_id},
    )
    assert saved.status_code == 200
    assert client.get(f"/api/datasets/{dataset_id}/resume").json() == {
        "dataset_id": dataset_id,
        "item_id": item_id,
        "anchor_id": anchor_id,
    }


@pytest.mark.parametrize("package_id", ["..", "%2E%2E", "C:%5Csecret"])
def test_import_identifier_rejects_traversal_and_absolute_forms(
    tmp_path: Path, package_id: str
) -> None:
    package_root = tmp_path / "packages"
    package_root.mkdir()
    repository = ReviewRepository(tmp_path / "review.sqlite3", package_root=package_root)
    client = TestClient(create_review_app(repository))
    headers = _session(client)
    response = client.post(f"/api/packages/{package_id}/import", headers=headers)
    assert response.status_code in {404, 422}


def test_pdf_hash_failure_is_visible_and_import_is_transactional(tmp_path: Path) -> None:
    package_root = tmp_path / "packages"
    package_root.mkdir()
    pdf_root = tmp_path / "pdf"
    _write_candidate(package_root, pdf_root)
    (pdf_root / "safe.pdf").write_bytes(b"changed")
    repository = ReviewRepository(
        tmp_path / "review.sqlite3",
        package_root=package_root,
        pdf_root=pdf_root,
    )
    client = TestClient(create_review_app(repository))
    card = client.get("/api/packages").json()[0]
    assert card["status"] == "invalid"
    assert card["pdf_hash_status"] == "fail"
    headers = _session(client)
    assert (
        client.post(f"/api/packages/{card['package_id']}/import", headers=headers).status_code
        == 422
    )
    assert client.get("/api/datasets").json() == []
