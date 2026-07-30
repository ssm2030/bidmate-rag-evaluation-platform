from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from bidmate_rag.eval_dataset.contract.models import Document, EvalItem, EvidenceAnchor
from bidmate_rag.eval_dataset.contract.validation import validate_package_records


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


def _anchor(document_id: UUID) -> EvidenceAnchor:
    return EvidenceAnchor(
        anchor_id=uuid4(),
        ordinal=0,
        document_id=document_id,
        pdf_page_number=1,
        printed_page_label=None,
        exact_quote="The required delivery date is 2026-08-31.",
        context_before=None,
        context_after=None,
        role="support",
        required=True,
        resolution_status="resolved",
        resolution_method="exact",
        document_sha256="a" * 64,
        resolver_version="v1",
        bbox=None,
    )


def test_answerable_item_requires_a_resolved_anchor() -> None:
    document = _document()
    item = EvalItem(
        item_id=uuid4(),
        revision=1,
        status="needs_review",
        question="What is the delivery date?",
        ground_truth_answer="2026-08-31",
        task_kind="extract",
        document_scope="single",
        answerability="answerable",
        evidence_mode="direct_quote",
        perturbation="none",
        metadata_filter={},
        history=[],
        verification_notes=[],
        provenance={},
        evidence_anchors=[_anchor(document.document_id)],
    )
    assert item.evidence_anchors[0].resolution_status == "resolved"


def test_unanswerable_item_rejects_evidence_anchor() -> None:
    document = _document()
    with pytest.raises(ValidationError, match="unanswerable"):
        EvalItem(
            item_id=uuid4(),
            revision=1,
            status="needs_review",
            question="What is the delivery date?",
            ground_truth_answer="Not stated",
            task_kind="extract",
            document_scope="single",
            answerability="unanswerable",
            evidence_mode="none",
            perturbation="none",
            metadata_filter={},
            history=[],
            verification_notes=[],
            provenance={},
            evidence_anchors=[_anchor(document.document_id)],
        )


def test_document_rejects_path_traversal() -> None:
    with pytest.raises(ValidationError, match="relative_pdf_path"):
        _document().model_copy(update={"relative_pdf_path": "../private.pdf"})
        Document(
            document_id=uuid4(),
            relative_pdf_path="../private.pdf",
            sha256="a" * 64,
            page_count=1,
            legacy_filename="private.pdf",
            external_ids={},
            source_classification="private",
            external_transmission_allowed=False,
        )


def test_package_validation_rejects_anchor_for_unknown_document() -> None:
    item = EvalItem(
        item_id=uuid4(),
        revision=1,
        status="needs_review",
        question="When?",
        ground_truth_answer="Tomorrow",
        task_kind="extract",
        document_scope="single",
        answerability="answerable",
        evidence_mode="direct_quote",
        perturbation="none",
        metadata_filter={},
        history=[],
        verification_notes=[],
        provenance={},
        evidence_anchors=[_anchor(uuid4())],
    )
    with pytest.raises(ValueError, match="unknown document"):
        validate_package_records([], [item])


def test_draft_allows_unresolved_anchor_but_approved_rejects_it() -> None:
    document = _document()
    unresolved = _anchor(document.document_id).model_copy(
        update={"resolution_status": "unresolved", "resolution_method": None}
    )
    payload = {
        "item_id": uuid4(),
        "revision": 1,
        "question": "What is the delivery date?",
        "ground_truth_answer": "2026-08-31",
        "task_kind": "extract",
        "document_scope": "single",
        "answerability": "answerable",
        "evidence_mode": "direct_quote",
        "perturbation": "none",
        "metadata_filter": {},
        "history": [],
        "verification_notes": [],
        "provenance": {},
        "evidence_anchors": [unresolved],
    }
    assert EvalItem(status="draft", **payload).evidence_anchors[0].resolution_status == "unresolved"
    with pytest.raises(ValidationError, match="approved items require resolved"):
        EvalItem(status="approved", **payload)
