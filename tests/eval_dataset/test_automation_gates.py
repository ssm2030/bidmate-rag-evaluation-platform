from __future__ import annotations

from dataclasses import replace
from uuid import NAMESPACE_URL, UUID, uuid5

from bidmate_rag.eval_dataset.automation.gates import CandidateGateContext, candidate_gate
from bidmate_rag.eval_dataset.contract.models import Document, EvalItem, EvidenceAnchor


def _document(name: str, digest: str) -> Document:
    return Document(
        document_id=uuid5(NAMESPACE_URL, f"document:{name}"),
        relative_pdf_path=f"{name}.pdf",
        sha256=digest,
        page_count=1,
        legacy_filename=f"{name}.pdf",
        external_ids={},
        source_classification="unknown",
        external_transmission_allowed=False,
    )


def _anchor(document: Document, quote: str, ordinal: int = 0) -> EvidenceAnchor:
    return EvidenceAnchor(
        anchor_id=uuid5(NAMESPACE_URL, f"anchor:{document.document_id}:{ordinal}:{quote}"),
        ordinal=ordinal,
        document_id=document.document_id,
        pdf_page_number=1,
        printed_page_label=None,
        exact_quote=quote,
        context_before=None,
        context_after=None,
        role="support",
        required=True,
        resolution_status="resolved",
        resolution_method="exact",
        document_sha256=document.sha256,
        resolver_version="automation-exact-v1",
        bbox=None,
    )


def _item(
    *,
    sop_type: str,
    question: str,
    anchors: list[EvidenceAnchor],
    answerability: str = "answerable",
    history: list[dict[str, str]] | None = None,
) -> EvalItem:
    contracts = {
        "A": ("extract", "single", "direct_quote", "none"),
        "B": ("compare", "multi", "multi_evidence", "none"),
        "C": ("follow_up", "single", "direct_quote", "none"),
        "D": ("extract", "single", "none", "none"),
        "E": ("extract", "single", "direct_quote", "typo"),
    }
    task_kind, scope, evidence_mode, perturbation = contracts[sop_type]
    return EvalItem(
        item_id=uuid5(NAMESPACE_URL, f"item:{sop_type}:{question}"),
        revision=1,
        status="needs_review",
        question=question,
        ground_truth_answer="원문에 근거한 답변입니다.",
        task_kind=task_kind,
        document_scope=scope,
        answerability=answerability,
        evidence_mode=evidence_mode,
        perturbation=perturbation,
        difficulty="medium",
        metadata_filter={},
        history=history or [],
        verification_notes=[],
        provenance={"sop_type": sop_type},
        evidence_anchors=anchors,
    )


def _context(
    documents: list[Document],
    quotes: dict[UUID, str],
    *,
    sop_type: str,
    primary_document_id: UUID | None = None,
    source_texts: tuple[str, ...] = (),
    absence_probe: str | None = None,
) -> CandidateGateContext:
    return CandidateGateContext(
        documents={document.document_id: document for document in documents},
        page_texts={
            document.document_id: (quotes.get(document.document_id, ""),) for document in documents
        },
        expected_sop_type=sop_type,
        institution_names=("테스트기관",),
        project_names=("디지털 전환 사업",),
        primary_document_id=primary_document_id,
        source_texts=source_texts,
        absence_probe=absence_probe,
    )


def test_answerable_gate_verifies_exact_quote_page_and_document_hash() -> None:
    document = _document("alpha", "a" * 64)
    quote = "계약기간은 착수일로부터 120일입니다."
    item = _item(
        sop_type="A",
        question="테스트기관 디지털 전환 사업의 계약기간은 어떻게 명시되어 있습니까?",
        anchors=[_anchor(document, quote)],
    )
    context = _context([document], {document.document_id: quote}, sop_type="A")

    assert candidate_gate(item, context=context).passed is True

    wrong_hash = item.model_copy(
        update={
            "evidence_anchors": [
                item.evidence_anchors[0].model_copy(update={"document_sha256": "b" * 64})
            ]
        }
    )
    wrong_quote = item.model_copy(
        update={
            "evidence_anchors": [
                item.evidence_anchors[0].model_copy(update={"exact_quote": "원문에 없는 문장"})
            ]
        }
    )
    assert "document_hash" in candidate_gate(wrong_hash, context=context).codes
    assert "exact_quote" in candidate_gate(wrong_quote, context=context).codes


def test_b_gate_requires_two_documents_and_primary_anchor_first() -> None:
    primary = _document("primary", "a" * 64)
    secondary = _document("secondary", "b" * 64)
    primary_quote = "주 사업기간은 계약일로부터 120일입니다."
    secondary_quote = "비교 사업기간은 계약일로부터 90일입니다."
    item = _item(
        sop_type="B",
        question="테스트기관 디지털 전환 사업과 비교 사업의 기간 차이는 무엇입니까?",
        anchors=[_anchor(secondary, secondary_quote, 0), _anchor(primary, primary_quote, 1)],
    )
    context = _context(
        [primary, secondary],
        {primary.document_id: primary_quote, secondary.document_id: secondary_quote},
        sop_type="B",
        primary_document_id=primary.document_id,
    )

    result = candidate_gate(item, context=context)

    assert result.passed is False
    assert "primary_anchor_order" in result.codes


def test_c_gate_requires_valid_conversation_history() -> None:
    document = _document("alpha", "a" * 64)
    quote = "제안서 제출기한은 8월 30일 17시입니다."
    item = _item(
        sop_type="C",
        question="테스트기관 디지털 전환 사업에서 그 제출기한은 언제입니까?",
        anchors=[_anchor(document, quote)],
    )
    context = _context([document], {document.document_id: quote}, sop_type="C")

    result = candidate_gate(item, context=context)

    assert result.passed is False
    assert "history" in result.codes


def test_d_gate_passes_only_when_absence_is_deterministically_proven() -> None:
    document = _document("alpha", "a" * 64)
    probe = "BM-ABSENT-9A63"
    item = _item(
        sop_type="D",
        question=f"테스트기관 디지털 전환 사업에 {probe} 발급번호가 있습니까?",
        anchors=[],
        answerability="unanswerable",
    )
    absent = _context(
        [document],
        {document.document_id: "계약기간은 120일입니다."},
        sop_type="D",
        source_texts=("계약기간은 120일입니다.",),
        absence_probe=probe,
    )
    present = replace(absent, source_texts=(f"발급번호 {probe}",))
    uncertain = replace(absent, source_texts=(), page_texts={document.document_id: ("",)})

    assert candidate_gate(item, context=absent).passed is True
    assert "absence_not_proven" in candidate_gate(item, context=present).codes
    uncertain_result = candidate_gate(item, context=uncertain)
    assert uncertain_result.passed is False
    assert uncertain_result.outcome == "review_required"


def test_gate_rejects_boilerplate_or_question_without_document_identity() -> None:
    document = _document("alpha", "a" * 64)
    quote = "계약기간은 120일입니다."
    item = _item(sop_type="A", question="Mock question 1?", anchors=[_anchor(document, quote)])
    context = _context([document], {document.document_id: quote}, sop_type="A")

    result = candidate_gate(item, context=context)

    assert result.passed is False
    assert {"boilerplate_question", "document_identity"} <= set(result.codes)
