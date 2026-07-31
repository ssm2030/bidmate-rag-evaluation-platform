from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from fastapi import FastAPI, HTTPException

from bidmate_rag.eval_dataset.contract.models import EvalItem, EvidenceAnchor
from bidmate_rag.eval_dataset.contract.package_io import read_package, write_package

from .gates import candidate_gate
from .inventory import BatchInventory, inventory_batch
from .ledger import AutomationLedger, CostLimitExceeded, OperationalCostCapExceeded, WorkUnit
from .live_context import (
    ContextWindow,
    build_context_windows,
    select_ranked_context_windows,
)
from .live_contracts import GeneratorOutput, ProviderUsage, StageName
from .live_service import LiveEvaluationService
from .planner import plan_sop_slots
from .schemas import (
    NormalizeStageRequest,
    PrepareStageRequest,
    ProviderFailureRequest,
    RunCreateRequest,
)
from .service import (
    CONTRACT_VERSION,
    PROMPT_BUNDLE_HASH,
    GeneratedCandidates,
    build_mock_candidates,
    build_run_identity,
)

_LIVE_CONTEXT_WINDOW_CHARS = 1_500
_LIVE_CONTEXT_MAX_WINDOWS = 8
_LIVE_CONTEXT_MAX_TOTAL_CHARS = 12_000


def _default_data_root() -> Path:
    return Path(__file__).resolve().parents[4].parent / "data"


def _http_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


def _provider_idempotency_key(
    *, run_id: str, work_unit_id: str, stage: StageName, request_hash: str
) -> str:
    canonical = "|".join((run_id, work_unit_id, stage, request_hash))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()




def _resolve_manifest_member(source_root: Path, value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"runtime manifest {label} must be a non-empty relative path")
    relative = Path(value)
    if relative.is_absolute():
        raise ValueError(f"runtime manifest {label} must be relative")
    resolved_root = source_root.resolve()
    resolved = (resolved_root / relative).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"runtime manifest {label} escapes the source root") from exc
    return resolved


def validate_live_source_manifest(
    inventory: BatchInventory,
    *,
    json_root: Path | str,
    pdf_root: Path | str,
) -> Path:
    """Reconcile the immutable runtime manifest with current parsed and PDF bytes."""
    json_base = Path(json_root).resolve()
    pdf_base = Path(pdf_root).resolve()
    if json_base.name != "Parsed" or pdf_base.name != "PDF1" or json_base.parent != pdf_base.parent:
        raise ValueError("live Parsed and PDF1 roots must share one source root")
    source_root = json_base.parent
    manifest_path = source_root / "runtime-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = manifest.get("sources") if isinstance(manifest, dict) else None
    schema_version = manifest.get("schema_version") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
        or not isinstance(records, list)
    ):
        raise ValueError("runtime manifest must use schema_version 1 with a sources array")
    if len(records) != len(inventory.documents):
        raise ValueError("runtime manifest source count does not match inventory")

    records_by_parsed: dict[Path, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("runtime manifest source entries must be objects")
        parsed_path = _resolve_manifest_member(source_root, record.get("parsed_file"), label="parsed_file")
        pdf_path = _resolve_manifest_member(source_root, record.get("pdf_file"), label="pdf_file")
        try:
            parsed_path.relative_to(json_base)
            pdf_path.relative_to(pdf_base)
        except ValueError as exc:
            raise ValueError("runtime manifest paths must remain under Parsed and PDF1") from exc
        if parsed_path in records_by_parsed:
            raise ValueError("runtime manifest parsed_file values must be unique")
        records_by_parsed[parsed_path] = {**record, "_pdf_path": pdf_path}

    matched: set[Path] = set()
    for document in inventory.documents:
        parsed_path = (json_base / document.relative_json_path).resolve()
        pdf_path = (pdf_base / document.relative_pdf_path).resolve()
        record = records_by_parsed.get(parsed_path)
        if record is None or record["_pdf_path"] != pdf_path:
            raise ValueError(f"runtime manifest path mapping is missing for {document.source_filename}")
        if (
            record.get("public_provenance_checked") is not True
            or record.get("empty_pages_within_threshold") is not True
        ):
            raise ValueError(f"runtime manifest provenance or page-quality gate failed for {document.source_filename}")
        current_sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        if record.get("pdf_sha256") != current_sha256 or current_sha256 != document.document_sha256:
            raise ValueError(f"runtime manifest PDF hash drift detected for {document.source_filename}")
        parsed_payload = json.loads(parsed_path.read_text(encoding="utf-8-sig"))
        pages = parsed_payload.get("pages") if isinstance(parsed_payload, dict) else None
        page_count = record.get("page_count")
        if (
            not isinstance(pages, list)
            or not isinstance(page_count, int)
            or isinstance(page_count, bool)
            or page_count != len(pages)
            or len(pages) != document.page_count
        ):
            raise ValueError(f"runtime manifest page count drift detected for {document.source_filename}")
        matched.add(parsed_path)
    if matched != set(records_by_parsed):
        raise ValueError("runtime manifest contains files outside the selected inventory")
    return manifest_path


def _normalized_candidate_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _candidate_evidence_identity(item: EvalItem) -> tuple[tuple[str, int, str], ...]:
    return tuple(
        sorted(
            (
                str(anchor.document_id),
                anchor.pdf_page_number,
                _normalized_candidate_text(anchor.exact_quote),
            )
            for anchor in item.evidence_anchors
        )
    )


def create_automation_app(
    *,
    mock_enabled: bool = False,
    live_enabled: bool = False,
    simulate_transient_failure: bool = False,
    ledger_path: Path | str | None = None,
    package_path: Path | str | None = None,
    batch_config_path: Path | str | None = None,
    json_root: Path | str | None = None,
    pdf_root: Path | str | None = None,
    inventory_cache_root: Path | str | None = None,
    provider_base_url: str = "https://api.openai.com/v1",
    stub_mode: bool = False,
    live_model_config_path: Path | str | None = None,
    live_prompt_root: Path | str | None = None,
) -> FastAPI:
    app = FastAPI(title="BidMate Evaluation Automation", version="2.0")
    data_root = _default_data_root()
    app.state.mock_enabled = bool(mock_enabled)
    app.state.live_enabled = bool(live_enabled)
    app.state.simulate_transient_failure = bool(simulate_transient_failure)
    app.state.ledger_path = Path(
        ledger_path
        or os.environ.get(
            "BIDMATE_EVAL_AUTOMATION_LEDGER",
            "artifacts/eval_dataset/rebuild/automation/ledger.sqlite3",
        )
    )
    app.state.package_path = Path(
        package_path
        or os.environ.get(
            "BIDMATE_EVAL_AUTOMATION_PACKAGE",
            "artifacts/eval_dataset/rebuild/automation/candidate-package",
        )
    )
    app.state.batch_config_path = Path(
        batch_config_path
        or os.environ.get(
            "BIDMATE_EVAL_BATCH_CONFIG",
            data_root / "Batch_config.json",
        )
    )
    app.state.json_root = Path(
        json_root or os.environ.get("BIDMATE_EVAL_JSON_ROOT", data_root / "Parsed")
    )
    app.state.pdf_root = Path(
        pdf_root or os.environ.get("BIDMATE_EVAL_PDF_ROOT", data_root / "PDF1")
    )
    app.state.inventory_cache_root = Path(
        inventory_cache_root
        or os.environ.get(
            "BIDMATE_EVAL_INVENTORY_CACHE",
            app.state.ledger_path.parent / "cache",
        )
    )
    app.state.ledger = AutomationLedger(app.state.ledger_path)
    app.state.live_service = (
        LiveEvaluationService.from_config_path(
            live_model_config_path
            or Path(__file__).resolve().parents[4] / "configs" / "eval_live_models.json",
            provider_base_url=provider_base_url,
            stub_mode=stub_mode,
            prompt_root=live_prompt_root,
        )
        if app.state.live_enabled
        else None
    )
    app.state.run_configs: dict[str, RunCreateRequest] = {}
    app.state.inventories: dict[str, BatchInventory] = {}
    app.state.generated: dict[str, GeneratedCandidates] = {}
    app.state.work_payloads: dict[str, dict[str, Any]] = {}
    app.state.work_contexts: dict[str, Any] = {}
    app.state.live_windows: dict[str, tuple[ContextWindow, ...]] = {}
    app.state.live_selected_windows: dict[str, tuple[ContextWindow, ...]] = {}
    app.state.live_drafts: dict[str, GeneratorOutput] = {}
    app.state.live_candidates: dict[str, EvalItem] = {}
    app.state.live_gates: dict[str, dict[str, Any]] = {}
    app.state.live_prepared: dict[str, dict[str, Any]] = {}
    app.state.live_repair_requests: dict[str, dict[str, Any]] = {}
    app.state.live_repair_counts: dict[str, int] = {}

    def require_run(run_id: str) -> tuple[RunCreateRequest, BatchInventory, GeneratedCandidates]:
        config = app.state.run_configs.get(run_id)
        inventory = app.state.inventories.get(run_id)
        generated = app.state.generated.get(run_id)
        if config is None or inventory is None or generated is None:
            raise _http_error(404, "run_not_loaded", "Create or resume the run first.")
        return config, inventory, generated

    def require_work(work_unit_id: str) -> tuple[dict[str, Any], EvalItem]:
        payload = app.state.work_payloads.get(work_unit_id)
        if payload is None:
            raise _http_error(
                404, "work_unit_not_loaded", "Plan the run before processing work units."
            )
        generated = app.state.generated[payload["run_id"]]
        item = generated.items[int(payload["ordinal"]) - 1]
        return payload, item

    def augment(payload: dict[str, Any], **updates: Any) -> dict[str, Any]:
        return {**payload, **updates}

    def require_live_service() -> LiveEvaluationService:
        service = app.state.live_service
        if service is None:
            raise _http_error(403, "live_not_configured", "Live service is not configured.")
        return service

    def live_windows_for(work_unit_id: str) -> tuple[ContextWindow, ...]:
        cached = app.state.live_windows.get(work_unit_id)
        if cached is not None:
            return cached
        require_work(work_unit_id)
        context = app.state.work_contexts[work_unit_id]
        windows: list[ContextWindow] = []
        for document_id, pages in sorted(
            context.page_texts.items(),
            key=lambda pair: (
                pair[0] != context.primary_document_id,
                str(pair[0]),
            ),
        ):
            windows.extend(
                build_context_windows(
                    str(document_id),
                    [
                        {"page_num": page_number, "text": text}
                        for page_number, text in enumerate(pages, start=1)
                    ],
                    max_chars=_LIVE_CONTEXT_WINDOW_CHARS,
                )
            )
        if not windows:
            raise _http_error(422, "live_context_empty", "No local context windows are available.")
        result = select_ranked_context_windows(
            windows,
            max_windows=_LIVE_CONTEXT_MAX_WINDOWS,
            max_total_chars=_LIVE_CONTEXT_MAX_TOTAL_CHARS,
        )
        if not result:
            raise _http_error(422, "live_context_budget_empty", "No context window fits the live request budget.")
        app.state.live_windows[work_unit_id] = result
        return result

    def require_live_work(work_unit_id: str) -> tuple[dict[str, Any], EvalItem, WorkUnit]:
        payload, item = require_work(work_unit_id)
        if payload["mode"] != "live":
            raise _http_error(409, "live_stage_requires_live_run", "The work unit is not live mode.")
        unit = app.state.ledger.claim(work_unit_id)
        if unit.status != "running":
            raise _http_error(409, "work_unit_not_claimable", f"Work unit is {unit.status}.")
        return payload, item, unit

    def resolve_live_evidence_anchors(
        *,
        work_unit_id: str,
        output: GeneratorOutput,
        windows: tuple[ContextWindow, ...],
        documents: tuple[Any, ...],
    ) -> list[EvidenceAnchor]:
        if output.type == "D":
            return []
        windows_by_id = {window.window_id: window for window in windows}
        documents_by_id = {str(document.document_id): document for document in documents}
        context = app.state.work_contexts[work_unit_id]
        pages_by_document = {
            str(document_id): pages for document_id, pages in context.page_texts.items()
        }
        anchors: list[EvidenceAnchor] = []
        for ordinal, claim in enumerate(output.evidence_claims):
            window = windows_by_id.get(claim.window_id)
            if window is None:
                raise ValueError("provider evidence window was not selected locally")
            if claim.quote not in window.source_text:
                raise ValueError("provider evidence quote cannot be resolved in the local source window")
            document = documents_by_id.get(window.document_id)
            if document is None:
                raise ValueError("provider evidence document is not in the local inventory")
            pages = pages_by_document.get(window.document_id)
            if pages is None or window.page_num > len(pages):
                raise ValueError("provider evidence page is not in the local inventory")
            page_text = pages[window.page_num - 1]
            if page_text.count(claim.quote) != 1:
                raise ValueError("provider evidence quote must resolve exactly once on the local source page")
            page_offset = page_text.find(claim.quote)
            anchor_identity = "|".join(
                (work_unit_id, str(ordinal), window.window_id, str(page_offset), claim.quote)
            )
            anchors.append(
                EvidenceAnchor(
                    anchor_id=uuid5(NAMESPACE_URL, f"bidmate:live-anchor:{anchor_identity}"),
                    ordinal=ordinal,
                    document_id=document.document_id,
                    pdf_page_number=window.page_num,
                    printed_page_label=None,
                    exact_quote=claim.quote,
                    context_before=(page_text[max(0, page_offset - 160) : page_offset] or None),
                    context_after=(
                        page_text[page_offset + len(claim.quote) : page_offset + len(claim.quote) + 160]
                        or None
                    ),
                    role="support",
                    required=True,
                    resolution_status="resolved",
                    resolution_method="exact",
                    document_sha256=document.sha256,
                    resolver_version="live-local-exact-v1",
                    bbox=None,
                )
            )
        return anchors

    @app.get("/v1/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ready",
            "contract_version": CONTRACT_VERSION,
            "mode": "mock" if app.state.mock_enabled else "live-disabled",
            "live_authorized": app.state.live_enabled,
        }

    @app.post("/v1/runs")
    def create_run(request: RunCreateRequest) -> dict[str, Any]:
        if request.mode == "live":
            if not app.state.live_enabled or not request.live_authorized:
                raise _http_error(
                    403,
                    "live_authorization_required",
                    "Live execution requires app enablement and an explicit authorization flag.",
                )
            if app.state.live_service is None:
                raise _http_error(403, "live_not_configured", "Live service is not configured.")
            if not app.state.live_service.stub_mode and not app.state.live_service.paid_execution_ready:
                raise _http_error(
                    403,
                    "model_price_reverification_required",
                    "Verify the current provider model and price before paid execution.",
                )
        elif not app.state.mock_enabled:
            raise _http_error(
                403,
                "mode_not_authorized",
                "Mock mode is not enabled.",
            )
        try:
            inventory = inventory_batch(
                app.state.batch_config_path,
                json_root=app.state.json_root,
                pdf_root=app.state.pdf_root,
                batch_id=request.batch_id,
                extraction_cache_root=app.state.inventory_cache_root,
            )
        except (OSError, ValueError) as exc:
            raise _http_error(422, "input_gate_failed", str(exc)) from exc
        if request.mode == "live":
            try:
                validate_live_source_manifest(
                    inventory,
                    json_root=app.state.json_root,
                    pdf_root=app.state.pdf_root,
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise _http_error(422, "live_source_manifest_invalid", str(exc)) from exc
        try:
            generated = build_mock_candidates(
                inventory,
                target_count=request.target_count,
            )
        except (OSError, ValueError) as exc:
            raise _http_error(422, "input_gate_failed", str(exc)) from exc
        identity = build_run_identity(
            inventory,
            target_count=request.target_count,
            mode=request.mode,
            campaign_key=request.campaign_key,
            data_root=request.data_root,
            prompt_bundle_hash=(
                require_live_service().prompt_bundle_hash if request.mode == "live" else None
            ),
        )
        if request.mode == "live":
            existing = app.state.ledger.run_id_for_identity(identity)
            if existing is None:
                campaign_id = app.state.ledger.create_campaign(
                    campaign_key=request.campaign_key or "",
                    cost_limit_microusd=request.cost_limit_microusd,
                )
                identity_json, identity_hash = app.state.ledger.identity_hash(identity)
                run_id = app.state.ledger.create_run(
                    f"batch-{request.batch_id}",
                    cost_limit_microusd=request.cost_limit_microusd,
                    identity_hash=identity_hash,
                    identity_json=identity_json,
                    mode="live",
                    campaign_id=campaign_id,
                )
            else:
                run_id = existing
        else:
            existing = app.state.ledger.run_id_for_identity(identity)
            run_id = app.state.ledger.get_or_create_run_for_identity(
                dataset_id=f"batch-{request.batch_id}",
                identity=identity,
                cost_limit_microusd=0,
            )
        app.state.run_configs[run_id] = request
        app.state.inventories[run_id] = inventory
        app.state.generated[run_id] = generated
        return {
            "run_id": run_id,
            "status": "authorized",
            "resumed": existing is not None,
            "batch_id": request.batch_id,
            "target_count": request.target_count,
            "mode": request.mode,
            "max_items_per_call": request.max_items_per_call,
        }

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        config, _, _ = require_run(run_id)
        summary = app.state.ledger.summary(run_id)
        if summary["status"] == "completed":
            public_status = "completed"
        elif (
            summary["candidate_count"] == config.target_count
            and summary["done_count"] == config.target_count
        ):
            public_status = "ready_to_finalize"
        elif summary["retryable_count"] or summary["permanent_failed_count"]:
            public_status = "blocked"
        else:
            public_status = "running"
        return {
            **summary,
            "status": public_status,
            "batch_id": config.batch_id,
            "target_count": config.target_count,
            "mode": config.mode,
        }

    @app.post("/v1/runs/{run_id}/inventory")
    def inventory_run(run_id: str, _: dict[str, Any] | None = None) -> dict[str, Any]:
        config, inventory, _ = require_run(run_id)
        return {
            "run_id": run_id,
            "status": "ready",
            "batch_id": config.batch_id,
            "target_count": config.target_count,
            "mode": config.mode,
            "document_count": len(inventory.documents),
            "documents": [
                {
                    "source_filename": document.source_filename,
                    "relative_pdf_path": document.relative_pdf_path,
                    "page_count": document.page_count,
                    "document_sha256": document.document_sha256,
                }
                for document in inventory.documents
            ],
        }

    @app.post("/v1/runs/{run_id}/plan")
    def plan_run(run_id: str, _: dict[str, Any] | None = None) -> dict[str, Any]:
        config, _, generated = require_run(run_id)
        work_units: list[dict[str, Any]] = []
        prompt_bundle_hash = (
            require_live_service().prompt_bundle_hash if config.mode == "live" else PROMPT_BUNDLE_HASH
        )
        for slot, item, context in zip(
            plan_sop_slots(config.target_count),
            generated.items,
            generated.contexts,
            strict=True,
        ):
            unit = app.state.ledger.create_work_unit(
                run_id,
                ordinal=slot.ordinal,
                plan={
                    "ordinal": slot.ordinal,
                    "sop_type": slot.sop_type,
                    "difficulty": slot.difficulty,
                },
                prompt_bundle_hash=prompt_bundle_hash,
            )
            payload = {
                "run_id": run_id,
                "work_unit_id": unit.work_unit_id,
                "ordinal": slot.ordinal,
                "sop_type": slot.sop_type,
                "difficulty": slot.difficulty,
                "mode": config.mode,
                "status": unit.status,
            }
            app.state.work_payloads[unit.work_unit_id] = payload
            app.state.work_contexts[unit.work_unit_id] = context
            work_units.append(payload)
        return {
            "run_id": run_id,
            "status": "planned",
            "mode": config.mode,
            "work_units": work_units,
        }

    def claim_stage(payload: dict[str, Any]) -> dict[str, Any]:
        work_unit_id = str(payload.get("work_unit_id", ""))
        canonical, _ = require_work(work_unit_id)
        unit = app.state.ledger.claim(work_unit_id)
        if unit.status == "done":
            return augment(
                canonical,
                status="already_done",
                candidate=app.state.ledger.work_unit_result(work_unit_id),
            )
        if unit.status != "running":
            return augment(canonical, status="blocked", terminal_status=unit.status)
        resume_stage = (
            unit.failure_stage
            if unit.attempts > 1 and unit.failure_stage in {"selector", "generator", "reviewer"}
            else "selector"
        )
        return augment(
            canonical,
            status="claimed",
            attempts=unit.attempts,
            resume_stage=resume_stage,
        )

    def selector_stage(payload: dict[str, Any]) -> dict[str, Any]:
        work_unit_id = str(payload.get("work_unit_id", ""))
        _, item = require_work(work_unit_id)
        if payload.get("mode") != "mock":
            raise _http_error(403, "mock_adapter_disabled", "Mock selector requires mock mode.")
        document_ids = [str(anchor.document_id) for anchor in item.evidence_anchors]
        if not document_ids:
            document_ids = [str(item.provenance["primary_document_id"])]
        return augment(
            payload,
            status="selected",
            selector={"document_ids": document_ids, "sop_type": item.provenance["sop_type"]},
        )

    def evidence_stage(payload: dict[str, Any]) -> dict[str, Any]:
        work_unit_id = str(payload.get("work_unit_id", ""))
        _, item = require_work(work_unit_id)
        return augment(
            payload,
            status="evidence_locked",
            evidence_anchors=[anchor.model_dump(mode="json") for anchor in item.evidence_anchors],
        )

    def generator_stage(payload: dict[str, Any]) -> dict[str, Any]:
        work_unit_id = str(payload.get("work_unit_id", ""))
        _, item = require_work(work_unit_id)
        if payload.get("mode") != "mock":
            raise _http_error(403, "mock_adapter_disabled", "Mock generator requires mock mode.")
        if (
            app.state.simulate_transient_failure
            and int(payload.get("ordinal", 0)) == 1
            and int(payload.get("attempts", 0)) == 1
        ):
            return augment(
                payload,
                status="adapter_failed",
                adapter_failure={
                    "retryable": True,
                    "stage": "generator",
                    "message": "simulated transient local adapter failure",
                },
            )
        return augment(
            payload,
            status="generated",
            candidate=item.model_dump(mode="json"),
        )

    def gate_stage(payload: dict[str, Any]) -> dict[str, Any]:
        work_unit_id = str(payload.get("work_unit_id", ""))
        require_work(work_unit_id)
        adapter_failure = payload.get("adapter_failure")
        if isinstance(adapter_failure, dict) and adapter_failure.get("retryable"):
            return augment(
                payload,
                status="blocked",
                gate={
                    "passed": False,
                    "codes": ["provider_retryable"],
                    "outcome": "retryable_failed",
                },
            )
        try:
            item = EvalItem.model_validate(payload.get("candidate"))
        except ValueError:
            return augment(
                payload,
                status="blocked",
                gate={"passed": False, "codes": ["schema_failed"], "outcome": "failed"},
            )
        result = candidate_gate(
            item,
            context=app.state.work_contexts[work_unit_id],
        )
        gated = augment(
            payload,
            status="gated" if result.passed else "blocked",
            gate={
                "passed": result.passed,
                "codes": result.codes,
                "outcome": result.outcome,
            },
        )
        if payload.get("mode") == "live":
            app.state.live_gates[work_unit_id] = gated["gate"]
        return gated

    def reviewer_stage(payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("mode") != "mock":
            raise _http_error(403, "mock_adapter_disabled", "Mock reviewer requires mock mode.")
        gate = payload.get("gate", {})
        passed = bool(gate.get("passed"))
        return augment(
            payload,
            status="reviewed" if passed else "blocked",
            review={
                "passed": passed,
                "fidelity": "pass" if passed else "fail",
                "clarity": "pass" if passed else "fail",
                "business_value": "pass" if passed else "fail",
                "answerability": "pass" if passed else "fail",
                "critical_findings": [],
            },
        )

    def terminal_stage(payload: dict[str, Any]) -> dict[str, Any]:
        work_unit_id = str(payload.get("work_unit_id", ""))
        require_work(work_unit_id)
        if app.state.ledger.work_unit_result(work_unit_id) is not None:
            return augment(payload, status="done", terminal_status="done")
        adapter_failure = payload.get("adapter_failure")
        if isinstance(adapter_failure, dict):
            app.state.ledger.record_failure(
                work_unit_id,
                error=str(adapter_failure.get("message", "provider adapter failure")),
                retryable=bool(adapter_failure.get("retryable")),
                failure_stage=str(adapter_failure.get("stage", "provider")),
            )
            terminal_status = (
                "retryable_failed" if adapter_failure.get("retryable") else "permanent_failed"
            )
            return augment(payload, status=terminal_status, terminal_status=terminal_status)
        gate = payload.get("gate", {})
        review = payload.get("review", {})
        if gate.get("outcome") == "review_required" or not review.get("passed"):
            app.state.ledger.mark_needs_review(
                work_unit_id,
                reason="deterministic or independent review requires human review",
            )
            return augment(payload, status="needs_review", terminal_status="needs_review")
        if not gate.get("passed"):
            app.state.ledger.record_failure(
                work_unit_id,
                error="deterministic candidate gate failed",
                retryable=False,
                failure_stage="deterministic_gate",
            )
            return augment(payload, status="permanent_failed", terminal_status="permanent_failed")
        candidate = EvalItem.model_validate(payload.get("candidate"))
        app.state.ledger.mark_done(
            work_unit_id,
            result=candidate.model_dump(mode="json"),
        )
        return augment(payload, status="done", terminal_status="done")

    def prepare_live_stage(
        work_unit_id: str, stage: StageName, request: PrepareStageRequest
    ) -> dict[str, Any]:
        unclaimed_payload, _ = require_work(work_unit_id)
        if unclaimed_payload["mode"] != "live":
            raise _http_error(409, "live_stage_requires_live_run", "The work unit is not live mode.")
        _, inventory, _ = require_run(unclaimed_payload["run_id"])
        try:
            validate_live_source_manifest(
                inventory,
                json_root=app.state.json_root,
                pdf_root=app.state.pdf_root,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise _http_error(422, "live_source_manifest_invalid", str(exc)) from exc

        payload, _, unit = require_live_work(work_unit_id)
        captured_selector_resume = (
            unit.attempts == 3
            and unit.last_error == "captured_provider_response_recovered"
            and unit.failure_stage == "generator"
            and stage in {"generator", "reviewer"}
        )
        captured_generator_resume = (
            unit.attempts == 3
            and unit.last_error == "captured_generator_response_recovered"
            and unit.failure_stage == "reviewer"
            and stage == "reviewer"
        )
        contract_repair_resume = (
            unit.attempts == 3
            and isinstance(unit.last_error, str)
            and unit.last_error.startswith("provider_output_repair:")
            and (
                (
                    unit.failure_stage == "selector"
                    and stage in {"selector", "generator", "reviewer"}
                )
                or (
                    unit.failure_stage == "generator"
                    and stage in {"generator", "reviewer"}
                )
            )
        )
        if request.attempt == 3 and not (
            captured_selector_resume
            or captured_generator_resume
            or contract_repair_resume
        ):
            raise _http_error(
                409,
                "captured_recovery_attempt_required",
                "Attempt 3 is reserved for a captured provider-response recovery.",
            )
        service = require_live_service()
        windows = live_windows_for(work_unit_id)
        repair_context: dict[str, Any] | None = None
        if (
            request.attempt in {2, 3}
            and unit.failure_stage == stage
            and isinstance(unit.last_error, str)
            and unit.last_error.startswith("provider_output_repair:")
        ):
            reason = unit.last_error.split(":", 2)[1]
            instruction = (
                "Return a corrected response that strictly satisfies the supplied "
                "JSON schema and local evidence contract."
            )
            if (
                stage == "selector"
                and "multi document_scope requires" in unit.last_error
            ):
                instruction += " Select evidence windows from at least two unique documents."
            elif (
                stage == "generator"
                and "Type D requires zero evidence claims" in unit.last_error
            ):
                instruction += (
                    " For Type D, evidence_claims must be [] and the answer must "
                    "state what is absent from the supplied context."
                )
            repair_context = {
                "reason": reason,
                "instruction": instruction,
            }
        if stage == "selector":
            prepared = service.prepare_selector(
                run_id=payload["run_id"],
                work_unit_id=work_unit_id,
                windows=windows,
                repair_context=repair_context,
            )
        elif stage == "generator":
            selected = app.state.live_selected_windows.get(work_unit_id)
            if not selected:
                raise _http_error(409, "selector_not_normalized", "Normalize selector before generator.")
            reviewer_repair = app.state.live_repair_requests.get(work_unit_id)
            prepared = service.prepare_generator(
                run_id=payload["run_id"],
                work_unit_id=work_unit_id,
                sop_type=payload["sop_type"],
                difficulty=payload["difficulty"],
                windows=selected,
                repair_context=reviewer_repair or repair_context,
            )
            windows = selected
        else:
            draft = app.state.live_drafts.get(work_unit_id)
            if draft is None:
                raise _http_error(409, "generator_not_normalized", "Normalize generator before reviewer.")
            selected = app.state.live_selected_windows.get(work_unit_id) or windows
            post_repair_review = None
            if request.attempt == 2 and app.state.live_repair_counts.get(work_unit_id, 0) > 0:
                post_repair_review = {
                    "reason": "post_generator_repair_review",
                    "instruction": "Review the repaired candidate independently and return a fresh decision.",
                }
            prepared = service.prepare_reviewer(
                run_id=payload["run_id"],
                work_unit_id=work_unit_id,
                draft=draft,
                windows=selected,
                repair_context=post_repair_review or repair_context,
            )
            windows = selected
        request_hash = hashlib.sha256(
            json.dumps(prepared.body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        try:
            call = app.state.ledger.reserve_provider_call(
                run_id=payload["run_id"],
                work_unit_id=work_unit_id,
                stage=stage,
                attempt=request.attempt,
                model=str(prepared.body["model"]),
                request_hash=request_hash,
                reserved_microusd=prepared.reserved_microusd,
                operational_cap_microusd=service.operational_cap_microusd,
            )
        except OperationalCostCapExceeded as exc:
            raise _http_error(
                409,
                "operational_cost_cap_reached",
                "The operational live-call ceiling blocks this provider request.",
            ) from exc
        except CostLimitExceeded as exc:
            raise _http_error(409, "cost_limit_exceeded", str(exc)) from exc

        app.state.live_prepared[call.provider_call_id] = {
            "stage": stage,
            "work_unit_id": work_unit_id,
            "windows": windows,
            "payload": dict(payload),
        }
        return {
            "provider_call_id": call.provider_call_id,
            "idempotency_key": _provider_idempotency_key(
                run_id=payload["run_id"],
                work_unit_id=work_unit_id,
                stage=stage,
                request_hash=request_hash,
            ),
            "stage": stage,
            "attempt": request.attempt,
            "reserved_microusd": call.reserved_microusd,
            "provider_request": {"url": prepared.url, "body": prepared.body},
        }

    def normalize_live_stage(
        work_unit_id: str, stage: StageName, request: NormalizeStageRequest
    ) -> dict[str, Any]:
        payload, planned_item, _ = require_live_work(work_unit_id)
        prepared = app.state.live_prepared.get(request.provider_call_id)
        if prepared is None or prepared["work_unit_id"] != work_unit_id or prepared["stage"] != stage:
            raise _http_error(404, "provider_call_not_prepared", "Provider call was not prepared here.")
        try:
            call = app.state.ledger.provider_call(request.provider_call_id)
        except ValueError as exc:
            raise _http_error(404, "provider_call_not_found", str(exc)) from exc
        if call.stage != stage or call.status != "reserved":
            raise _http_error(409, "provider_call_not_pending", "Provider call is not pending normalization.")
        service = require_live_service()
        try:
            if stage == "selector":
                result = service.normalize_selector(
                    provider_payload=request.provider_payload,
                    allowed_windows={window.window_id: window for window in prepared["windows"]},
                )
            elif stage == "generator":
                result = service.normalize_generator(
                    provider_payload=request.provider_payload,
                    allowed_windows={window.window_id: window for window in prepared["windows"]},
                )
            else:
                result = service.normalize_reviewer(provider_payload=request.provider_payload)
        except ValueError as exc:
            response_reconciled = False
            try:
                usage = ProviderUsage.model_validate(request.provider_payload.get("usage", {}))
                response_id = request.provider_payload.get("id")
                if not isinstance(response_id, str) or not response_id:
                    raise ValueError("provider response id is required")
                app.state.ledger.reconcile_provider_call(
                    provider_call_id=request.provider_call_id,
                    status="succeeded",
                    actual_microusd=service.actual_cost_microusd(
                        stage=stage,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                    ),
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    provider_response_id=response_id,
                    error_code="invalid_response",
                )
                response_reconciled = True
            except ValueError:
                try:
                    app.state.ledger.mark_provider_call_unknown(
                        request.provider_call_id, error_code="invalid_response"
                    )
                except ValueError:
                    pass
            retryable = response_reconciled and call.attempt == 1
            app.state.ledger.record_failure(
                work_unit_id,
                error=f"provider_output_repair:invalid_provider_response:{exc}",
                retryable=retryable,
                failure_stage=stage,
            )
            raise _http_error(422, "invalid_provider_response", str(exc)) from exc

        actual_microusd = service.actual_cost_microusd(
            stage=stage,
            input_tokens=result.usage_input_tokens,
            output_tokens=result.usage_output_tokens,
        )
        try:
            app.state.ledger.reconcile_provider_call(
                provider_call_id=request.provider_call_id,
                status="succeeded",
                actual_microusd=actual_microusd,
                input_tokens=result.usage_input_tokens,
                output_tokens=result.usage_output_tokens,
                provider_response_id=result.response_id,
            )
        except ValueError as exc:
            raise _http_error(422, "provider_usage_rejected", str(exc)) from exc

        canonical = dict(prepared["payload"])
        canonical["attempts"] = call.attempt
        response = {
            "provider_call_id": request.provider_call_id,
            "stage": stage,
            "provider_response_id": result.response_id,
            "actual_microusd": actual_microusd,
        }
        if stage == "selector":
            app.state.live_selected_windows[work_unit_id] = result.selected_windows
            return augment(
                canonical,
                **response,
                status="selected",
                selector={
                    "window_ids": [window.window_id for window in result.selected_windows],
                },
            )
        if stage == "generator":
            if (
                result.output.type != canonical["sop_type"]
                or result.output.difficulty != canonical["difficulty"]
            ):
                app.state.ledger.record_failure(
                    work_unit_id,
                    error="planned_contract_mismatch",
                    retryable=False,
                    failure_stage=stage,
                )
                raise _http_error(
                    422,
                    "planned_contract_mismatch",
                    "Provider output does not match the planned type and difficulty.",
                )
            run_config, _, generated = require_run(canonical["run_id"])
            try:
                anchors = resolve_live_evidence_anchors(
                    work_unit_id=work_unit_id,
                    output=result.output,
                    windows=prepared["windows"],
                    documents=tuple(generated.documents),
                )
            except ValueError as exc:
                app.state.ledger.record_failure(
                    work_unit_id,
                    error=f"provider_output_repair:candidate_contract_invalid:{exc}",
                    retryable=call.attempt == 1,
                    failure_stage=stage,
                )
                raise _http_error(422, "candidate_contract_invalid", str(exc)) from exc
            candidate_data = planned_item.model_dump(mode="json")
            candidate_data.update(
                question=result.output.question,
                ground_truth_answer=result.output.answer,
                difficulty=result.output.difficulty,
                verification_notes=["live provider output resolved against local selected evidence windows"],
                provenance={
                    "mode": "live",
                    "provider": "responses_api",
                    "provider_model": service.policies[stage].model,
                    "provider_response_id": result.response_id,
                    "provider_call_id": request.provider_call_id,
                    "prompt_bundle_hash": service.prompt_bundle_hash,
                    "batch_id": run_config.batch_id,
                    "sop_type": result.output.type,
                    "selected_window_ids": [window.window_id for window in prepared["windows"]],
                },
                evidence_anchors=[anchor.model_dump(mode="json") for anchor in anchors],
            )
            try:
                candidate = EvalItem.model_validate(candidate_data)
            except ValueError as exc:
                app.state.ledger.record_failure(
                    work_unit_id,
                    error=f"provider_output_repair:candidate_contract_invalid:{exc}",
                    retryable=call.attempt == 1,
                    failure_stage=stage,
                )
                raise _http_error(422, "candidate_contract_invalid", str(exc)) from exc
            candidate_question = _normalized_candidate_text(candidate.question)
            candidate_answer = _normalized_candidate_text(candidate.ground_truth_answer)
            candidate_evidence = _candidate_evidence_identity(candidate)
            for other_work_unit_id, other in app.state.live_candidates.items():
                if other_work_unit_id == work_unit_id:
                    continue
                other_payload = app.state.work_payloads.get(other_work_unit_id)
                if other_payload is None or other_payload["run_id"] != canonical["run_id"]:
                    continue
                same_question = _normalized_candidate_text(other.question) == candidate_question
                same_answer_and_evidence = (
                    _normalized_candidate_text(other.ground_truth_answer) == candidate_answer
                    and _candidate_evidence_identity(other) == candidate_evidence
                )
                if same_question or same_answer_and_evidence:
                    message = "live candidate duplicates an earlier candidate in the same run"
                    app.state.ledger.record_failure(
                        work_unit_id,
                        error=f"provider_output_repair:candidate_contract_invalid:{message}",
                        retryable=call.attempt == 1,
                        failure_stage=stage,
                    )
                    raise _http_error(422, "candidate_contract_invalid", message)
            app.state.live_drafts[work_unit_id] = result.output
            app.state.live_candidates[work_unit_id] = candidate
            app.state.live_repair_requests.pop(work_unit_id, None)
            app.state.live_gates.pop(work_unit_id, None)
            return augment(
                canonical,
                **response,
                status="generated",
                candidate=candidate.model_dump(mode="json"),
            )

        candidate = app.state.live_candidates.get(work_unit_id)
        if candidate is None:
            raise _http_error(409, "review_context_missing", "Candidate is required before review.")
        gate = app.state.live_gates.get(work_unit_id)
        if gate is None:
            gate_result = candidate_gate(
                candidate,
                context=app.state.work_contexts[work_unit_id],
            )
            gate = {
                "passed": gate_result.passed,
                "codes": gate_result.codes,
                "outcome": gate_result.outcome,
            }
            app.state.live_gates[work_unit_id] = gate
        review_passed = (
            result.output.decision == "accept"
            and result.output.factuality == "pass"
            and result.output.answerability == "pass"
            and result.output.evidence_coverage == "pass"
        )
        issues = [issue.model_dump(mode="json") for issue in result.output.issues]
        repair_count = app.state.live_repair_counts.get(work_unit_id, 0)
        status = "reviewed" if review_passed else "blocked"
        response_attempts = call.attempt
        if result.output.decision == "repair" and repair_count == 0:
            repair_count = 1
            app.state.live_repair_counts[work_unit_id] = repair_count
            app.state.live_repair_requests[work_unit_id] = {
                "reason": "reviewer_requested_generator_repair",
                "issues": issues,
                "instruction": "Revise the candidate once to address every reviewer issue while preserving exact local evidence.",
            }
            app.state.live_gates.pop(work_unit_id, None)
            status = "repair_requested"
            response_attempts = 2
        return augment(
            canonical,
            **response,
            status=status,
            attempts=response_attempts,
            repair_count=repair_count,
            candidate=candidate.model_dump(mode="json"),
            gate=gate,
            review={
                "passed": review_passed,
                "decision": result.output.decision,
                "factuality": result.output.factuality,
                "answerability": result.output.answerability,
                "evidence_coverage": result.output.evidence_coverage,
                "issues": issues,
            },
        )

    @app.post("/v1/workflow/claim-work-unit")
    def workflow_claim(payload: dict[str, Any]) -> dict[str, Any]:
        return claim_stage(payload)

    @app.post("/v1/workflow/mock-selector")
    def workflow_selector(payload: dict[str, Any]) -> dict[str, Any]:
        return selector_stage(payload)

    @app.post("/v1/workflow/lock-evidence")
    def workflow_evidence(payload: dict[str, Any]) -> dict[str, Any]:
        return evidence_stage(payload)

    @app.post("/v1/workflow/mock-generator")
    def workflow_generator(payload: dict[str, Any]) -> dict[str, Any]:
        return generator_stage(payload)

    @app.post("/v1/workflow/deterministic-gates")
    def workflow_gates(payload: dict[str, Any]) -> dict[str, Any]:
        return gate_stage(payload)

    @app.post("/v1/workflow/mock-reviewer")
    def workflow_reviewer(payload: dict[str, Any]) -> dict[str, Any]:
        return reviewer_stage(payload)

    @app.post("/v1/workflow/record-terminal-outcome")
    def workflow_terminal(payload: dict[str, Any]) -> dict[str, Any]:
        return terminal_stage(payload)

    @app.post("/v1/work-units/{work_unit_id}/claim")
    def claim(work_unit_id: str) -> dict[str, Any]:
        payload, _ = require_work(work_unit_id)
        return claim_stage(payload)

    @app.post("/v1/work-units/{work_unit_id}/selector/prepare")
    def prepare_selector(work_unit_id: str, request: PrepareStageRequest) -> dict[str, Any]:
        return prepare_live_stage(work_unit_id, "selector", request)

    @app.post("/v1/work-units/{work_unit_id}/selector/normalize")
    def normalize_selector(work_unit_id: str, request: NormalizeStageRequest) -> dict[str, Any]:
        return normalize_live_stage(work_unit_id, "selector", request)

    @app.post("/v1/work-units/{work_unit_id}/generator/prepare")
    def prepare_generator(work_unit_id: str, request: PrepareStageRequest) -> dict[str, Any]:
        return prepare_live_stage(work_unit_id, "generator", request)

    @app.post("/v1/work-units/{work_unit_id}/generator/normalize")
    def normalize_generator(work_unit_id: str, request: NormalizeStageRequest) -> dict[str, Any]:
        return normalize_live_stage(work_unit_id, "generator", request)

    @app.post("/v1/work-units/{work_unit_id}/reviewer/prepare")
    def prepare_reviewer(work_unit_id: str, request: PrepareStageRequest) -> dict[str, Any]:
        return prepare_live_stage(work_unit_id, "reviewer", request)

    @app.post("/v1/work-units/{work_unit_id}/reviewer/normalize")
    def normalize_reviewer(work_unit_id: str, request: NormalizeStageRequest) -> dict[str, Any]:
        return normalize_live_stage(work_unit_id, "reviewer", request)

    @app.post("/v1/provider-calls/{provider_call_id}/failure")
    def provider_failure(
        provider_call_id: str, request: ProviderFailureRequest
    ) -> dict[str, Any]:
        try:
            call = app.state.ledger.provider_call(provider_call_id)
            if request.failure_class == "invalid_response" and call.status in {"succeeded", "unknown"}:
                retryable = call.status == "succeeded" and any(
                    unit.work_unit_id == call.work_unit_id
                    for unit in app.state.ledger.retryable_units(call.run_id)
                )
                return {"provider_call_id": provider_call_id, "retryable": retryable}
            if request.failure_class in {"definite_rejection", "rate_limited", "transient_server"}:
                app.state.ledger.reconcile_provider_call(
                    provider_call_id=provider_call_id,
                    status="released",
                    error_code=request.error_code,
                )
            else:
                app.state.ledger.mark_provider_call_unknown(
                    provider_call_id, error_code=request.error_code
                )
            retryable = request.failure_class in {"rate_limited", "transient_server"} and call.attempt == 1
            app.state.ledger.record_failure(
                call.work_unit_id,
                error=request.error_code,
                retryable=retryable,
                failure_stage=call.stage,
            )
        except ValueError as exc:
            raise _http_error(409, "provider_failure_rejected", str(exc)) from exc
        return {"provider_call_id": provider_call_id, "retryable": retryable}

    @app.get("/v1/runs/{run_id}/costs")
    def run_costs(run_id: str) -> dict[str, int]:
        try:
            totals = app.state.ledger.get_cost_totals(run_id)
        except ValueError as exc:
            raise _http_error(404, "live_costs_not_found", str(exc)) from exc
        return {
            "actual_microusd": totals.actual_microusd,
            "open_reserved_microusd": totals.open_reserved_microusd,
            "effective_microusd": totals.effective_microusd,
            "cost_limit_microusd": totals.cost_limit_microusd,
        }

    @app.post("/v1/work-units/{work_unit_id}/evidence-lock")
    def evidence_lock(work_unit_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return evidence_stage(augment(payload, work_unit_id=work_unit_id))

    @app.post("/v1/work-units/{work_unit_id}/deterministic-gate")
    def deterministic_gate(work_unit_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return gate_stage(augment(payload, work_unit_id=work_unit_id))

    @app.post("/v1/work-units/{work_unit_id}/terminal")
    def terminal(work_unit_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return terminal_stage(augment(payload, work_unit_id=work_unit_id))

    @app.post("/v1/work-units/{work_unit_id}/selector/reconcile-captured")
    def reconcile_captured_selector(
        work_unit_id: str, request: NormalizeStageRequest
    ) -> dict[str, Any]:
        canonical, _ = require_work(work_unit_id)
        if canonical["mode"] != "live":
            raise _http_error(
                409, "live_stage_requires_live_run", "The work unit is not live mode."
            )
        try:
            call = app.state.ledger.provider_call(request.provider_call_id)
        except ValueError as exc:
            raise _http_error(404, "provider_call_not_found", str(exc)) from exc
        if call.work_unit_id != work_unit_id or call.stage != "selector":
            raise _http_error(
                404, "provider_call_mismatch", "Provider call does not match this selector."
            )
        prepared = app.state.live_prepared.get(request.provider_call_id)
        windows = (
            prepared["windows"]
            if prepared is not None
            and prepared["work_unit_id"] == work_unit_id
            and prepared["stage"] == "selector"
            else live_windows_for(work_unit_id)
        )
        service = require_live_service()
        try:
            result = service.normalize_selector(
                provider_payload=request.provider_payload,
                allowed_windows={window.window_id: window for window in windows},
            )
            actual_microusd = service.actual_cost_microusd(
                stage="selector",
                input_tokens=result.usage_input_tokens,
                output_tokens=result.usage_output_tokens,
            )
            app.state.ledger.recover_captured_selector_response(
                provider_call_id=request.provider_call_id,
                actual_microusd=actual_microusd,
                input_tokens=result.usage_input_tokens,
                output_tokens=result.usage_output_tokens,
                provider_response_id=result.response_id,
            )
        except ValueError as exc:
            raise _http_error(422, "captured_response_rejected", str(exc)) from exc
        app.state.live_selected_windows[work_unit_id] = result.selected_windows
        return augment(
            canonical,
            provider_call_id=request.provider_call_id,
            stage="selector",
            provider_response_id=result.response_id,
            actual_microusd=actual_microusd,
            status="selected",
            selector={
                "window_ids": [window.window_id for window in result.selected_windows]
            },
        )

    @app.post("/v1/work-units/{work_unit_id}/generator/reconcile-captured")
    def reconcile_captured_generator(
        work_unit_id: str, request: NormalizeStageRequest
    ) -> dict[str, Any]:
        canonical, _ = require_work(work_unit_id)
        if canonical["mode"] != "live":
            raise _http_error(
                409, "live_stage_requires_live_run", "The work unit is not live mode."
            )
        selected = app.state.live_selected_windows.get(work_unit_id)
        if not selected:
            raise _http_error(
                409,
                "selector_not_normalized",
                "Rehydrate the captured selector response before the generator response.",
            )
        try:
            call = app.state.ledger.provider_call(request.provider_call_id)
        except ValueError as exc:
            raise _http_error(404, "provider_call_not_found", str(exc)) from exc
        if call.work_unit_id != work_unit_id or call.stage != "generator":
            raise _http_error(
                404, "provider_call_mismatch", "Provider call does not match this generator."
            )
        service = require_live_service()
        try:
            preview = service.normalize_generator(
                provider_payload=request.provider_payload,
                allowed_windows={window.window_id: window for window in selected},
            )
            actual_microusd = service.actual_cost_microusd(
                stage="generator",
                input_tokens=preview.usage_input_tokens,
                output_tokens=preview.usage_output_tokens,
            )
            app.state.ledger.reopen_captured_generator_response(
                provider_call_id=request.provider_call_id,
                actual_microusd=actual_microusd,
                input_tokens=preview.usage_input_tokens,
                output_tokens=preview.usage_output_tokens,
                provider_response_id=preview.response_id,
            )
        except ValueError as exc:
            raise _http_error(422, "captured_response_rejected", str(exc)) from exc
        app.state.live_prepared[request.provider_call_id] = {
            "stage": "generator",
            "work_unit_id": work_unit_id,
            "windows": selected,
            "payload": dict(canonical),
        }
        normalized = normalize_live_stage(work_unit_id, "generator", request)
        try:
            app.state.ledger.complete_captured_generator_replay(
                request.provider_call_id
            )
        except ValueError as exc:
            raise _http_error(409, "captured_replay_incomplete", str(exc)) from exc
        return normalized

    @app.post("/v1/runs/{run_id}/requeue-released-auth-failures")
    def requeue_released_auth_failures(run_id: str, limit: int = 1) -> dict[str, Any]:
        require_run(run_id)
        try:
            units = app.state.ledger.requeue_released_auth_failures(run_id, limit=limit)
        except ValueError as exc:
            raise _http_error(422, "auth_recovery_rejected", str(exc)) from exc
        work_units = []
        for unit in units:
            payload = dict(app.state.work_payloads.get(unit.work_unit_id, {}))
            payload.update(
                work_unit_id=unit.work_unit_id,
                status=unit.status,
                failure_stage=unit.failure_stage,
            )
            work_units.append(payload)
        return {
            "run_id": run_id,
            "requeued_count": len(work_units),
            "work_units": work_units,
        }

    @app.post("/v1/runs/{run_id}/requeue-post-auth-contract-failures")
    def requeue_post_auth_contract_failures(
        run_id: str, limit: int = 2
    ) -> dict[str, Any]:
        require_run(run_id)
        try:
            units = app.state.ledger.requeue_post_auth_contract_failures(
                run_id, limit=limit
            )
        except ValueError as exc:
            raise _http_error(422, "contract_repair_rejected", str(exc)) from exc
        work_units = []
        for unit in units:
            payload = dict(app.state.work_payloads.get(unit.work_unit_id, {}))
            payload.update(
                work_unit_id=unit.work_unit_id,
                status=unit.status,
                failure_stage=unit.failure_stage,
            )
            work_units.append(payload)
        return {
            "run_id": run_id,
            "requeued_count": len(work_units),
            "work_units": work_units,
        }

    @app.get("/v1/runs/{run_id}/retryable")
    def retryable(run_id: str) -> list[dict[str, Any]]:
        require_run(run_id)
        units = app.state.ledger.retryable_units(run_id)
        return [
            {
                **app.state.work_payloads[unit.work_unit_id],
                "status": "retryable_failed",
                "failure_stage": unit.failure_stage,
            }
            for unit in units
        ]

    @app.post("/v1/runs/{run_id}/finalize")
    def finalize(run_id: str) -> dict[str, Any]:
        config, _, generated = require_run(run_id)
        summary = app.state.ledger.summary(run_id)
        if (
            summary["candidate_count"] != config.target_count
            or summary["done_count"] != config.target_count
        ):
            raise _http_error(
                409,
                "batch_not_ready",
                "All planned work units must be done before package finalization.",
            )
        items: list[EvalItem] = []
        work_payloads = sorted(
            (
                payload
                for payload in app.state.work_payloads.values()
                if payload["run_id"] == run_id
            ),
            key=lambda payload: int(payload["ordinal"]),
        )
        for payload in work_payloads:
            result = app.state.ledger.work_unit_result(payload["work_unit_id"])
            if result is None:
                raise _http_error(409, "missing_result", "A done work unit has no result.")
            items.append(EvalItem.model_validate(result))
        if config.mode == "live":
            seen_questions: set[str] = set()
            seen_answer_evidence: set[tuple[str, tuple[tuple[str, int, str], ...]]] = set()
            for item in items:
                question_identity = _normalized_candidate_text(item.question)
                answer_evidence_identity = (
                    _normalized_candidate_text(item.ground_truth_answer),
                    _candidate_evidence_identity(item),
                )
                if (
                    question_identity in seen_questions
                    or answer_evidence_identity in seen_answer_evidence
                ):
                    raise _http_error(
                        409,
                        "duplicate_candidates",
                        "Live package contains duplicate candidate content.",
                    )
                seen_questions.add(question_identity)
                seen_answer_evidence.add(answer_evidence_identity)
        if app.state.package_path.exists():
            loaded = read_package(app.state.package_path)
            if len(loaded["items"]) != config.target_count:
                raise _http_error(
                    409, "package_conflict", "Existing package has a different item count."
                )
        else:
            write_package(
                app.state.package_path,
                dataset_id=uuid5(NAMESPACE_URL, f"bidmate:n8n:{run_id}"),
                documents=generated.documents,
                items=items,
            )
            read_package(app.state.package_path)
        checksum = hashlib.sha256(
            (app.state.package_path / "manifest.json").read_bytes()
        ).hexdigest()
        app.state.ledger.record_package_checksum(run_id, checksum)
        final_summary = app.state.ledger.summary(run_id)
        return {
            "run_id": run_id,
            "status": "completed",
            "item_count": len(items),
            "anchor_count": sum(len(item.evidence_anchors) for item in items),
            "package_name": app.state.package_path.name,
            "package_checksum": checksum,
            "schema_status": "pass",
            "pdf_hash_status": "pass",
            "done_count": final_summary["done_count"],
        }

    return app


app = create_automation_app(
    mock_enabled=os.environ.get("BIDMATE_EVAL_MODE", "mock").lower() == "mock",
    live_enabled=os.environ.get("BIDMATE_EVAL_MODE", "mock").lower() == "live",
    provider_base_url=os.environ.get("BIDMATE_OPENAI_BASE_URL", "https://api.openai.com/v1"),
    stub_mode=os.environ.get("BIDMATE_EVAL_STUB_MODE", "false").lower() == "true",
    simulate_transient_failure=(
        os.environ.get("BIDMATE_EVAL_SIMULATE_TRANSIENT_FAILURE", "false").lower() == "true"
    ),
)
