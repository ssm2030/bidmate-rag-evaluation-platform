from __future__ import annotations

import csv
from uuid import uuid4

from bidmate_rag.eval_dataset.contract.legacy_export import LEGACY_COLUMNS, export_legacy
from bidmate_rag.eval_dataset.contract.models import Document, EvalItem


def _document() -> Document:
    return Document(
        document_id=uuid4(),
        relative_pdf_path="public/rfp.pdf",
        sha256="a" * 64,
        page_count=2,
        legacy_filename="rfp.pdf",
        external_ids={},
        source_classification="public",
        external_transmission_allowed=False,
    )


def _item(document: Document, *, answerability: str) -> EvalItem:
    return EvalItem.model_validate(
        {
            "item_id": str(uuid4()),
            "revision": 1,
            "status": "approved",
            "question": "What?",
            "ground_truth_answer": "Answer",
            "task_kind": "extract",
            "document_scope": "single",
            "answerability": answerability,
            "evidence_mode": "none" if answerability == "unanswerable" else "direct_quote",
            "perturbation": "none",
            "difficulty": "medium",
            "metadata_filter": {"year": 2026},
            "history": [],
            "verification_notes": ["reviewed"],
            "provenance": {},
            "evidence_anchors": []
            if answerability == "unanswerable"
            else [
                {
                    "anchor_id": str(uuid4()),
                    "ordinal": 0,
                    "document_id": str(document.document_id),
                    "pdf_page_number": 2,
                    "printed_page_label": None,
                    "exact_quote": "Answer",
                    "context_before": None,
                    "context_after": None,
                    "role": "support",
                    "required": True,
                    "resolution_status": "resolved",
                    "resolution_method": "exact",
                    "document_sha256": document.sha256,
                    "resolver_version": "v1",
                    "bbox": None,
                }
            ],
        }
    )


def test_legacy_export_separates_standard_and_safety_rows(tmp_path) -> None:
    document = _document()
    paths = export_legacy(
        tmp_path,
        [
            _item(document, answerability="answerable"),
            _item(document, answerability="unanswerable"),
        ],
        {document.document_id: document},
    )
    with paths.standard.open(newline="", encoding="utf-8-sig") as handle:
        standard = list(csv.DictReader(handle))
    with paths.safety.open(newline="", encoding="utf-8-sig") as handle:
        safety = list(csv.DictReader(handle))
    assert list(standard[0]) == LEGACY_COLUMNS
    assert len(standard) == 1 and standard[0]["ground_truth_docs"] == '["rfp.pdf"]'
    assert standard[0]["source_pages"] == "[2]"
    assert standard[0]["reasoning_process"] == ""
    assert len(safety) == 1 and safety[0]["type"] == "D"
