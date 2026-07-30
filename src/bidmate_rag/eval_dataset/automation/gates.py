from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Sequence
from uuid import UUID

from bidmate_rag.eval_dataset.contract.models import Document, EvalItem


@dataclass(frozen=True)
class CandidateGateContext:
    documents: Mapping[UUID, Document]
    page_texts: Mapping[UUID, Sequence[str]]
    expected_sop_type: str
    institution_names: tuple[str, ...]
    project_names: tuple[str, ...]
    primary_document_id: UUID | None = None
    source_texts: tuple[str, ...] = ()
    absence_probe: str | None = None


@dataclass(frozen=True)
class GateResult:
    passed: bool
    codes: list[str]
    outcome: str


_SOP_CONTRACTS = {
    "A": ("extract", "single", "answerable", "direct_quote", "none"),
    "B": ("compare", "multi", "answerable", "multi_evidence", "none"),
    "C": ("follow_up", "single", "answerable", "direct_quote", "none"),
    "D": ("extract", "single", "unanswerable", "none", "none"),
    "E": ("extract", "single", "answerable", "direct_quote", "typo"),
}
_BOILERPLATE = re.compile(
    r"(mock\s+(question|answer|evidence)|question\s*\d+|todo|placeholder)",
    re.IGNORECASE,
)


def _append_once(codes: list[str], code: str) -> None:
    if code not in codes:
        codes.append(code)


def _valid_history(item: EvalItem) -> bool:
    if len(item.history) < 2:
        return False
    roles = [entry.get("role") for entry in item.history]
    return (
        roles[0] == "user"
        and roles[1] == "assistant"
        and all(entry.get("content", "").strip() for entry in item.history[:2])
    )


def candidate_gate(
    item: EvalItem,
    *,
    context: CandidateGateContext | None = None,
) -> GateResult:
    codes: list[str] = []
    review_required = False
    if item.status == "approved":
        codes.append("candidate_status")
    if _BOILERPLATE.search(item.question):
        codes.append("boilerplate_question")
    if any(
        anchor.required and anchor.resolution_status != "resolved"
        for anchor in item.evidence_anchors
    ):
        codes.append("required_anchor_resolved")

    if context is not None:
        contract = _SOP_CONTRACTS.get(context.expected_sop_type)
        actual = (
            item.task_kind,
            item.document_scope,
            item.answerability,
            item.evidence_mode,
            item.perturbation,
        )
        if contract is None or actual != contract:
            codes.append("sop_contract")
        names = tuple(
            value.casefold()
            for value in (*context.institution_names, *context.project_names)
            if value.strip()
        )
        if not any(name in item.question.casefold() for name in names):
            codes.append("document_identity")

        if context.expected_sop_type == "B":
            document_ids = [anchor.document_id for anchor in item.evidence_anchors]
            if not 2 <= len(set(document_ids)) <= 3:
                codes.append("multi_document_count")
            if (
                context.primary_document_id is None
                or not document_ids
                or document_ids[0] != context.primary_document_id
            ):
                codes.append("primary_anchor_order")
        if context.expected_sop_type == "C" and not _valid_history(item):
            codes.append("history")

        for anchor in item.evidence_anchors:
            document = context.documents.get(anchor.document_id)
            if document is None:
                _append_once(codes, "document_reference")
                continue
            if anchor.document_sha256 != document.sha256:
                _append_once(codes, "document_hash")
            pages = context.page_texts.get(anchor.document_id, ())
            if not 1 <= anchor.pdf_page_number <= len(pages):
                _append_once(codes, "pdf_page")
            elif anchor.exact_quote not in pages[anchor.pdf_page_number - 1]:
                _append_once(codes, "exact_quote")

        if context.expected_sop_type == "D":
            if item.evidence_anchors or item.evidence_mode != "none":
                codes.append("unanswerable_anchor_contract")
            searchable = tuple(context.source_texts) + tuple(
                page for pages in context.page_texts.values() for page in pages
            )
            probe = (context.absence_probe or "").strip()
            if not probe or not any(text.strip() for text in searchable):
                codes.append("absence_not_proven")
                review_required = True
            elif any(probe.casefold() in text.casefold() for text in searchable):
                codes.append("absence_not_proven")
                review_required = True

    return GateResult(
        passed=not codes,
        codes=codes,
        outcome="review_required" if review_required else ("done" if not codes else "failed"),
    )
