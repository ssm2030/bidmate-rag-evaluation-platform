from __future__ import annotations

import json
from pathlib import Path

import pytest

from bidmate_rag.eval_dataset.review.db import ApprovalBlockedError, ReviewConflictError
from bidmate_rag.eval_dataset.review.repository import ReviewRepository
from bidmate_rag.eval_dataset.review.resolver_service import resolve_local_pdf_path


def _repository(tmp_path: Path, schema_v2_fixture: Path) -> ReviewRepository:
    repository = ReviewRepository(tmp_path / "review.sqlite3")
    repository.import_package(schema_v2_fixture)
    return repository


def test_import_list_and_get_preserve_schema_v2_package(
    tmp_path: Path, schema_v2_fixture: Path
) -> None:
    repository = _repository(tmp_path, schema_v2_fixture)

    datasets = repository.list_datasets()
    assert len(datasets) == 1
    assert datasets[0]["schema_version"] == "2.0.0"
    item = repository.list_items(datasets[0]["dataset_id"])[0]
    loaded = repository.get_item(item["item_id"])

    assert loaded["item_id"] == item["item_id"]
    assert loaded["revision"] == 1
    assert loaded["status"] == "needs_review"


def test_draft_save_is_optimistic_and_approval_creates_immutable_snapshot(
    tmp_path: Path, schema_v2_fixture: Path
) -> None:
    repository = _repository(tmp_path, schema_v2_fixture)
    item = repository.list_items(repository.list_datasets()[0]["dataset_id"])[0]

    saved = repository.save_draft(
        item["item_id"],
        base_revision=1,
        patch={"question": "Edited question?"},
    )
    assert saved["revision"] == 2
    assert saved["status"] == "draft"
    with pytest.raises(ReviewConflictError):
        repository.save_draft(item["item_id"], base_revision=1, patch={"question": "stale"})

    approved = repository.approve(item["item_id"], base_revision=2)
    assert approved["status"] == "approved"
    snapshot = repository.list_snapshots(item["item_id"])[0]
    assert snapshot["action"] == "approved"
    assert json.loads(snapshot["item_json"])["question"] == "Edited question?"
    with pytest.raises(PermissionError):
        repository.save_draft(
            item["item_id"], base_revision=approved["revision"], patch={"question": "no"}
        )


def test_unresolved_or_changed_anchor_blocks_approval_until_manual_resolution(
    tmp_path: Path, schema_v2_fixture: Path
) -> None:
    repository = _repository(tmp_path, schema_v2_fixture)
    item = repository.list_items(repository.list_datasets()[0]["dataset_id"])[0]
    anchor = item["evidence_anchors"][0]
    changed = repository.save_draft(
        item["item_id"],
        base_revision=1,
        patch={
            "evidence_anchors": [
                {
                    **anchor,
                    "resolution_status": "document_changed",
                    "resolution_method": None,
                }
            ]
        },
    )
    with pytest.raises(ApprovalBlockedError, match="document_changed"):
        repository.approve(item["item_id"], base_revision=changed["revision"])

    resolved = repository.resolve_anchor(
        item["item_id"],
        anchor["anchor_id"],
        base_revision=changed["revision"],
        method="manual",
        selected_quote=anchor["exact_quote"],
        page_number=anchor["pdf_page_number"],
        bbox={
            "x0": 0.1,
            "y0": 0.1,
            "x1": 0.2,
            "y1": 0.2,
            "coordinate_space": "normalized_top_left",
            "page_width": 612,
            "page_height": 792,
            "rotation": 0,
        },
    )
    assert resolved["evidence_anchors"][0]["resolution_method"] == "manual"
    assert (
        repository.approve(item["item_id"], base_revision=resolved["revision"])["status"]
        == "approved"
    )


def test_fork_and_reject_create_new_immutable_review_history(
    tmp_path: Path, schema_v2_fixture: Path
) -> None:
    repository = _repository(tmp_path, schema_v2_fixture)
    item = repository.list_items(repository.list_datasets()[0]["dataset_id"])[0]
    approved = repository.approve(item["item_id"], base_revision=1)

    fork = repository.fork(item["item_id"], base_revision=approved["revision"])
    assert fork["item_id"] != item["item_id"]
    assert fork["status"] == "draft"
    rejected = repository.reject(fork["item_id"], base_revision=1, reason="duplicate")
    assert rejected["status"] == "rejected"
    assert repository.list_snapshots(fork["item_id"])[0]["action"] == "rejected"


def test_pdf_path_is_confined_to_configured_local_root(tmp_path: Path) -> None:
    root = tmp_path / "pdf-root"
    root.mkdir()
    file = root / "safe.pdf"
    file.write_bytes(b"%PDF")

    assert resolve_local_pdf_path(root, "safe.pdf") == file
    with pytest.raises(ValueError, match="outside"):
        resolve_local_pdf_path(root, "../outside.pdf")
