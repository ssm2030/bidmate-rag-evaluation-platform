from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from bidmate_rag.eval_dataset.contract.models import (
    Document,
    EvalItem,
    EvidenceAnchor,
)
from bidmate_rag.eval_dataset.contract.package_io import read_package, write_package

from .gates import CandidateGateContext, candidate_gate
from .inventory import BatchInventory, InventoryDocument, inventory_batch
from .ledger import AutomationLedger
from .planner import SopSlot, plan_sop_slots

MOCK_CANDIDATE_COUNT = 30
PROMPT_BUNDLE_HASH = "sop-local-deterministic-v2"
CONTRACT_VERSION = "bidmate-eval-automation-v2"
MOCK_PROVIDER_MODEL = "local-deterministic-v2"


@dataclass(frozen=True)
class GeneratedCandidates:
    documents: list[Document]
    items: list[EvalItem]
    contexts: list[CandidateGateContext]


@dataclass(frozen=True)
class MockBatchSummary:
    run_id: str
    candidate_count: int
    created_count: int
    done_count: int
    retry_count: int
    status: str
    package_checksum: str | None


@dataclass(frozen=True)
class _LockedEvidence:
    page_number: int
    exact_quote: str


def can_export_candidate(
    item: EvalItem,
    *,
    context: CandidateGateContext | None = None,
) -> bool:
    return candidate_gate(item, context=context).passed


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _candidate_lines(text: str) -> list[str]:
    candidates: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        normalized = _normalize_text(line)
        if (
            18 <= len(normalized) <= 360
            and not re.fullmatch(r"[-–—·.\s\d]+", line)
            and not line.startswith("|")
        ):
            candidates.append(line)
    return candidates


def _locked_evidence(document: InventoryDocument, selector: int) -> _LockedEvidence:
    candidates: list[tuple[int, str]] = []
    for page_index, pdf_text in enumerate(document.page_texts):
        source_text = (
            document.source_page_texts[page_index]
            if page_index < len(document.source_page_texts)
            else ""
        )
        normalized_source = _normalize_text(source_text)
        if not normalized_source:
            continue
        for line in _candidate_lines(pdf_text):
            if _normalize_text(line) in normalized_source:
                candidates.append((page_index + 1, line))
    if not candidates:
        raise ValueError(
            f"no exact PDF quote can be locked to selected JSON source: {document.source_filename}"
        )
    candidates.sort(
        key=lambda candidate: (
            candidate[0] <= 2,
            abs(120 - len(candidate[1])),
            candidate[0],
            candidate[1],
        )
    )
    page_number, exact_quote = candidates[selector % len(candidates)]
    return _LockedEvidence(page_number, exact_quote)


def _document_model(batch_id: int, source: InventoryDocument) -> Document:
    document_id = uuid5(
        NAMESPACE_URL,
        f"bidmate:batch:{batch_id}:{source.relative_pdf_path}:{source.document_sha256}",
    )
    return Document(
        document_id=document_id,
        relative_pdf_path=source.relative_pdf_path,
        sha256=source.document_sha256,
        page_count=source.page_count,
        legacy_filename=Path(source.relative_pdf_path).name,
        external_ids={
            "batch_id": str(batch_id),
            "source_fingerprint": source.source_fingerprint,
        },
        source_classification="unknown",
        external_transmission_allowed=False,
    )


def _anchor(
    document: Document,
    *,
    slot: SopSlot,
    ordinal: int,
    evidence: _LockedEvidence,
) -> EvidenceAnchor:
    return EvidenceAnchor(
        anchor_id=uuid5(
            NAMESPACE_URL,
            f"bidmate:anchor:{slot.ordinal}:{ordinal}:{document.document_id}:"
            f"{evidence.page_number}:{evidence.exact_quote}",
        ),
        ordinal=ordinal,
        document_id=document.document_id,
        pdf_page_number=evidence.page_number,
        printed_page_label=None,
        exact_quote=evidence.exact_quote,
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


def _cue(quote: str) -> str:
    compact = _normalize_text(quote)
    return compact[:42].rstrip(" ,.;:")


def _answerable_content(
    slot: SopSlot,
    primary: InventoryDocument,
    secondary: InventoryDocument | None,
    evidence: Sequence[_LockedEvidence],
) -> tuple[str, str, list[dict[str, str]]]:
    cue = _cue(evidence[0].exact_quote)
    if slot.sop_type == "A":
        question = (
            f"{primary.institution_name}의 {primary.project_name} 공고에서 "
            f"‘{cue}’와 관련해 명시한 내용은 무엇입니까?"
        )
        return question, evidence[0].exact_quote, []
    if slot.sop_type == "B" and secondary is not None:
        question = (
            f"{primary.institution_name}의 {primary.project_name}와 "
            f"{secondary.institution_name}의 {secondary.project_name}에서 "
            "각각 확인되는 핵심 조건을 비교해 주세요."
        )
        answer = (
            f"{primary.project_name}: {evidence[0].exact_quote}\n"
            f"{secondary.project_name}: {evidence[1].exact_quote}"
        )
        return question, answer, []
    if slot.sop_type == "C":
        history = [
            {
                "role": "user",
                "content": (
                    f"{primary.institution_name}의 {primary.project_name} 공고에서 "
                    "핵심 사업 조건 하나를 알려주세요."
                ),
            },
            {"role": "assistant", "content": evidence[0].exact_quote},
        ]
        question = (
            f"{primary.institution_name}의 {primary.project_name}에서 "
            f"그 조건과 연결된 ‘{cue}’의 구체적 내용은 무엇입니까?"
        )
        return question, evidence[0].exact_quote, history
    if slot.sop_type == "E":
        question = (
            f"{primary.institution_name}의 {primary.project_name} 공고에서 "
            f"‘{cue}’ 관련 핵심 내요은 무엇입니까?"
        )
        return question, evidence[0].exact_quote, []
    raise ValueError(f"unsupported answerable SOP type: {slot.sop_type}")


def _context(
    inventory: BatchInventory,
    source_by_id: dict[UUID, InventoryDocument],
    document_by_id: dict[UUID, Document],
    item: EvalItem,
    slot: SopSlot,
    primary_document_id: UUID,
    absence_probe: str | None,
) -> CandidateGateContext:
    selected_ids = {anchor.document_id for anchor in item.evidence_anchors}
    if not selected_ids:
        selected_ids = {primary_document_id}
    selected_sources = [source_by_id[document_id] for document_id in selected_ids]
    return CandidateGateContext(
        documents=document_by_id,
        page_texts={
            document_id: source_by_id[document_id].page_texts for document_id in selected_ids
        },
        expected_sop_type=slot.sop_type,
        institution_names=tuple(source.institution_name for source in selected_sources),
        project_names=tuple(source.project_name for source in selected_sources),
        primary_document_id=primary_document_id,
        source_texts=tuple(
            text for source in selected_sources for text in source.source_page_texts
        ),
        absence_probe=absence_probe,
    )


def build_mock_candidates(
    inventory: BatchInventory,
    *,
    target_count: int = MOCK_CANDIDATE_COUNT,
) -> GeneratedCandidates:
    if len(inventory.documents) < 2:
        raise ValueError("SOP B comparison requires at least two Batch documents")
    slots = plan_sop_slots(target_count)
    documents = [_document_model(inventory.batch_id, source) for source in inventory.documents]
    document_by_id = {document.document_id: document for document in documents}
    source_by_id = {
        document.document_id: source
        for document, source in zip(documents, inventory.documents, strict=True)
    }
    items: list[EvalItem] = []
    contexts: list[CandidateGateContext] = []
    for slot in slots:
        primary_index = (slot.ordinal - 1) % len(inventory.documents)
        secondary_index = (primary_index + 1) % len(inventory.documents)
        primary_source = inventory.documents[primary_index]
        primary_document = documents[primary_index]
        secondary_source = inventory.documents[secondary_index]
        secondary_document = documents[secondary_index]
        absence_probe: str | None = None

        if slot.sop_type == "D":
            absence_probe = (
                "BM-ABSENT-"
                + hashlib.sha256(f"{primary_source.source_fingerprint}:{slot.ordinal}".encode())
                .hexdigest()[:10]
                .upper()
            )
            question = (
                f"{primary_source.institution_name}의 {primary_source.project_name} 공고에서 "
                f"해외 현지법인 납세증명서 발급번호 {absence_probe}를 확인할 수 있습니까?"
            )
            answer = f"선택된 원문에는 {absence_probe} 정보가 없어 답할 수 없습니다."
            history: list[dict[str, str]] = []
            anchors: list[EvidenceAnchor] = []
        else:
            primary_evidence = _locked_evidence(primary_source, slot.ordinal)
            evidence = [primary_evidence]
            if slot.sop_type == "B":
                evidence.append(_locked_evidence(secondary_source, slot.ordinal + 1))
            question, answer, history = _answerable_content(
                slot,
                primary_source,
                secondary_source if slot.sop_type == "B" else None,
                evidence,
            )
            anchors = [
                _anchor(
                    primary_document,
                    slot=slot,
                    ordinal=0,
                    evidence=primary_evidence,
                )
            ]
            if slot.sop_type == "B":
                anchors.append(
                    _anchor(
                        secondary_document,
                        slot=slot,
                        ordinal=1,
                        evidence=evidence[1],
                    )
                )

        item = EvalItem(
            item_id=uuid5(
                NAMESPACE_URL,
                f"bidmate:item:{inventory.batch_id}:{slot.ordinal}:{primary_document.document_id}",
            ),
            revision=1,
            status="needs_review",
            question=question,
            ground_truth_answer=answer,
            task_kind=slot.task_kind,
            document_scope="multi" if slot.document_count > 1 else "single",
            answerability=slot.answerability,
            evidence_mode=slot.evidence_mode,
            perturbation=slot.perturbation,
            difficulty=slot.difficulty,
            metadata_filter={"batch_id": inventory.batch_id},
            history=history,
            verification_notes=["deterministic local adapter; exact evidence locked"],
            provenance={
                "mode": "mock",
                "provider_model": MOCK_PROVIDER_MODEL,
                "batch_id": inventory.batch_id,
                "sop_type": slot.sop_type,
                "slot_ordinal": slot.ordinal,
                "primary_document_id": str(primary_document.document_id),
                "source_fingerprints": [
                    primary_source.source_fingerprint,
                    *([secondary_source.source_fingerprint] if slot.sop_type == "B" else []),
                ],
                "absence_probe": absence_probe,
            },
            evidence_anchors=anchors,
        )
        context = _context(
            inventory,
            source_by_id,
            document_by_id,
            item,
            slot,
            primary_document.document_id,
            absence_probe,
        )
        result = candidate_gate(item, context=context)
        if not result.passed:
            raise ValueError(
                f"deterministic mock candidate {slot.ordinal} failed gates: "
                f"{','.join(result.codes)}"
            )
        items.append(item)
        contexts.append(context)
    return GeneratedCandidates(documents, items, contexts)


def _package_checksum(package_path: Path) -> str:
    return hashlib.sha256((package_path / "manifest.json").read_bytes()).hexdigest()


def _slot_plan_hash(target_count: int) -> str:
    payload = [
        {
            "ordinal": slot.ordinal,
            "sop_type": slot.sop_type,
            "task_kind": slot.task_kind,
            "difficulty": slot.difficulty,
            "document_count": slot.document_count,
            "answerability": slot.answerability,
            "evidence_mode": slot.evidence_mode,
            "requires_history": slot.requires_history,
            "perturbation": slot.perturbation,
        }
        for slot in plan_sop_slots(target_count)
    ]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _document_set_hash(inventory: BatchInventory) -> str:
    payload = "\n".join(
        sorted(
            f"{document.relative_pdf_path}:{document.document_sha256}:{document.source_fingerprint}"
            for document in inventory.documents
        )
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def build_run_identity(
    inventory: BatchInventory,
    *,
    target_count: int,
    mode: str = "mock",
) -> dict[str, int | str]:
    if mode != "mock":
        raise ValueError("live run identity requires separately approved provider configuration")
    return {
        "batch_id": inventory.batch_id,
        "mode": mode,
        "slot_plan_hash": _slot_plan_hash(target_count),
        "prompt_bundle_hash": PROMPT_BUNDLE_HASH,
        "contract_version": CONTRACT_VERSION,
        "provider_model": MOCK_PROVIDER_MODEL,
        "document_set_hash": _document_set_hash(inventory),
    }


def _summary(
    ledger: AutomationLedger,
    run_id: str,
    *,
    created_count: int,
) -> MockBatchSummary:
    state = ledger.summary(run_id)
    return MockBatchSummary(
        run_id=run_id,
        candidate_count=int(state["candidate_count"]),
        created_count=created_count,
        done_count=int(state["done_count"]),
        retry_count=int(state["retry_count"]),
        status=str(state["status"]),
        package_checksum=(
            state["package_checksum"] if isinstance(state["package_checksum"], str) else None
        ),
    )


def _default_data_root() -> Path:
    return Path(__file__).resolve().parents[4].parent / "data"


def run_mock_batch(
    ledger_path: Path | str,
    package_path: Path | str,
    *,
    batch_config_path: Path | str | None = None,
    json_root: Path | str | None = None,
    pdf_root: Path | str | None = None,
    batch_id: int = 1,
    target_count: int = MOCK_CANDIDATE_COUNT,
    extraction_cache_root: Path | str | None = None,
) -> MockBatchSummary:
    """Run or resume the deterministic zero-cost SOP batch using actual Batch JSON/PDF inputs."""
    data_root = _default_data_root()
    batch_config_path = Path(
        batch_config_path
        or os.environ.get("BIDMATE_EVAL_BATCH_CONFIG", data_root / "Batch_config.json")
    )
    json_root = Path(json_root or os.environ.get("BIDMATE_EVAL_JSON_ROOT", data_root / "Parsed"))
    pdf_root = Path(pdf_root or os.environ.get("BIDMATE_EVAL_PDF_ROOT", data_root / "PDF1"))
    package_path = Path(package_path)
    inventory = inventory_batch(
        batch_config_path,
        json_root=json_root,
        pdf_root=pdf_root,
        batch_id=batch_id,
        extraction_cache_root=(extraction_cache_root or Path(ledger_path).parent / "cache"),
    )
    identity = build_run_identity(
        inventory,
        target_count=target_count,
        mode="mock",
    )
    dataset_name = f"batch-{batch_id}"
    ledger = AutomationLedger(ledger_path)
    run_id = ledger.get_or_create_run_for_identity(
        dataset_id=dataset_name,
        identity=identity,
        cost_limit_microusd=0,
    )

    if package_path.exists():
        loaded = read_package(package_path)
        if len(loaded["items"]) != target_count:
            raise ValueError("existing package does not contain the expected candidate count")
        ledger.record_package_checksum(run_id, _package_checksum(package_path))
        summary = _summary(ledger, run_id, created_count=0)
        if summary.done_count != target_count or summary.status != "completed":
            raise ValueError("existing package has no matching completed ledger run")
        return summary

    generated = build_mock_candidates(inventory, target_count=target_count)
    slots = plan_sop_slots(target_count)
    for slot, item in zip(slots, generated.items, strict=True):
        work = ledger.create_work_unit(
            run_id,
            ordinal=slot.ordinal,
            plan={
                "ordinal": slot.ordinal,
                "sop_type": slot.sop_type,
                "difficulty": slot.difficulty,
            },
            prompt_bundle_hash=PROMPT_BUNDLE_HASH,
        )
        if work.status == "done":
            continue
        claimed = ledger.claim(work.work_unit_id)
        if claimed.status == "retryable_failed":
            claimed = ledger.claim(work.work_unit_id)
        if slot.ordinal == 1 and claimed.attempts == 1:
            ledger.record_failure(
                work.work_unit_id,
                error="simulated transient local adapter failure",
                retryable=True,
                failure_stage="generator",
            )
            claimed = ledger.claim(work.work_unit_id)
        if claimed.status != "running":
            raise RuntimeError(f"work unit {slot.ordinal} was not claimable: {claimed.status}")
        ledger.mark_done(
            work.work_unit_id,
            result=item.model_dump(mode="json"),
        )

    before_package = _summary(ledger, run_id, created_count=0)
    if before_package.done_count != target_count or before_package.status != "awaiting_package":
        raise RuntimeError("ledger did not complete every work unit")
    dataset_id = uuid5(
        NAMESPACE_URL,
        "bidmate:dataset:"
        + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),
    )
    write_package(
        package_path,
        dataset_id=dataset_id,
        documents=generated.documents,
        items=generated.items,
    )
    read_package(package_path)
    ledger.record_package_checksum(run_id, _package_checksum(package_path))
    return _summary(ledger, run_id, created_count=target_count)
