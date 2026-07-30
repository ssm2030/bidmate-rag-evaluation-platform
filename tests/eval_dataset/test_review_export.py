from __future__ import annotations

from pathlib import Path

import pytest

from bidmate_rag.eval_dataset.review.db import ApprovalBlockedError
from bidmate_rag.eval_dataset.review.repository import ReviewRepository


def test_export_blocks_pending_items_and_writes_only_approved_snapshot(
    tmp_path: Path, schema_v2_fixture: Path
) -> None:
    export_root = tmp_path / "configured-exports"
    repository = ReviewRepository(
        tmp_path / "review.sqlite3",
        export_root=export_root,
    )
    dataset_id = repository.import_package(schema_v2_fixture)["dataset_id"]
    item = repository.list_items(dataset_id)[0]

    with pytest.raises(ApprovalBlockedError, match="unresolved"):
        repository.export_legacy(dataset_id)

    repository.approve(item["item_id"], base_revision=1)
    exported = repository.export_legacy(dataset_id, actor_session_id="reviewer-1")
    assert exported.standard.exists()
    assert exported.safety.exists()
    assert exported.standard.is_relative_to(export_root)
    assert exported.safety.is_relative_to(export_root)
    assert "Edited" not in exported.standard.read_text(encoding="utf-8-sig")

    first_standard = exported.standard.read_bytes()
    first_safety = exported.safety.read_bytes()
    repeated = repository.export_legacy(dataset_id, actor_session_id="reviewer-1")
    assert repeated.export_id == exported.export_id
    assert repeated.checksum == exported.checksum
    assert repeated.standard.read_bytes() == first_standard
    assert repeated.safety.read_bytes() == first_safety

    export_rows = repository.database.connection.execute(
        "SELECT export_id, checksum, item_count, relative_path "
        "FROM review_exports WHERE dataset_id=?",
        (dataset_id,),
    ).fetchall()
    assert [dict(row) for row in export_rows] == [
        {
            "export_id": exported.export_id,
            "checksum": exported.checksum,
            "item_count": 1,
            "relative_path": exported.relative_path,
        }
    ]


def test_export_requires_server_configured_root(tmp_path: Path, schema_v2_fixture: Path) -> None:
    repository = ReviewRepository(tmp_path / "review.sqlite3")
    dataset_id = repository.import_package(schema_v2_fixture)["dataset_id"]
    item = repository.list_items(dataset_id)[0]
    repository.approve(item["item_id"], base_revision=1)

    with pytest.raises(ValueError, match="export root"):
        repository.export_legacy(dataset_id)
