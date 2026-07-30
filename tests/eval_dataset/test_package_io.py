from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from bidmate_rag.eval_dataset.contract.models import Document, EvalItem, EvidenceAnchor
from bidmate_rag.eval_dataset.contract.package_io import read_package, write_package


def _records() -> tuple[Document, EvalItem]:
    document = Document(
        document_id=uuid4(),
        relative_pdf_path="public/rfp.pdf",
        sha256="a" * 64,
        page_count=2,
        legacy_filename="rfp.pdf",
        external_ids={},
        source_classification="public",
        external_transmission_allowed=False,
    )
    anchor = EvidenceAnchor(
        anchor_id=uuid4(),
        ordinal=0,
        document_id=document.document_id,
        pdf_page_number=1,
        printed_page_label=None,
        exact_quote="Delivery date is 2026-08-31.",
        context_before=None,
        context_after=None,
        role="support",
        required=True,
        resolution_status="resolved",
        resolution_method="exact",
        document_sha256=document.sha256,
        resolver_version="v1",
        bbox=None,
    )
    item = EvalItem(
        item_id=uuid4(),
        revision=1,
        status="needs_review",
        question="What is the date?",
        ground_truth_answer="2026-08-31",
        task_kind="extract",
        document_scope="single",
        answerability="answerable",
        evidence_mode="direct_quote",
        perturbation="none",
        metadata_filter={},
        history=[],
        verification_notes=["checked"],
        provenance={},
        evidence_anchors=[anchor],
    )
    return document, item


def test_package_round_trip_writes_canonical_checksums(tmp_path) -> None:
    document, item = _records()
    package = tmp_path / "dataset"
    write_package(package, dataset_id=uuid4(), documents=[document], items=[item])
    loaded = read_package(package)
    assert loaded["documents"][0]["document_id"] == str(document.document_id)
    assert loaded["items"][0]["item_id"] == str(item.item_id)
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    assert {entry["path"] for entry in manifest["files"]} == {
        "documents.jsonl",
        "items.jsonl",
        "generation_events.jsonl",
        "checksums.json",
    }


def test_package_reader_rejects_checksum_tampering(tmp_path) -> None:
    document, item = _records()
    package = tmp_path / "dataset"
    write_package(package, dataset_id=uuid4(), documents=[document], items=[item])
    (package / "items.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        read_package(package)


def test_frozen_fixture_has_complete_files_and_strict_schemas(tmp_path) -> None:
    source = Path("tests/eval_dataset/fixtures/schema_v2_valid")
    expected_files = {
        "manifest.json",
        "documents.jsonl",
        "items.jsonl",
        "generation_events.jsonl",
        "checksums.json",
    }
    assert {path.name for path in source.iterdir()} == expected_files
    fixture = tmp_path / "schema_v2_valid"
    fixture.mkdir()
    for name in expected_files:
        data = (source / name).read_bytes().replace(b"\r\n", b"\n")
        (fixture / name).write_bytes(data)
    loaded = read_package(fixture)
    assert loaded["manifest"]["schema_version"] == "2.0.0"
    assert len(loaded["documents"]) == len(loaded["items"]) == 1
    root = Path("schemas/eval_dataset/v2")
    for name in {"manifest", "document", "item", "generation_event", "review_event", "checksums"}:
        schema = json.loads((root / f"{name}.schema.json").read_text(encoding="utf-8"))
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["additionalProperties"] is False


def test_package_reader_rejects_unexpected_file_before_accepting_package(tmp_path) -> None:
    document, item = _records()
    package = tmp_path / "dataset"
    write_package(package, dataset_id=uuid4(), documents=[document], items=[item])
    (package / "unexpected.txt").write_text("not part of the contract", encoding="utf-8")
    with pytest.raises(ValueError, match="file set"):
        read_package(package)
