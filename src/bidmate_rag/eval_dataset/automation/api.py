from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from fastapi import FastAPI, HTTPException

from bidmate_rag.eval_dataset.contract.models import EvalItem
from bidmate_rag.eval_dataset.contract.package_io import read_package, write_package

from .gates import candidate_gate
from .inventory import BatchInventory, inventory_batch
from .ledger import AutomationLedger
from .planner import plan_sop_slots
from .schemas import RunCreateRequest
from .service import (
    CONTRACT_VERSION,
    PROMPT_BUNDLE_HASH,
    GeneratedCandidates,
    build_mock_candidates,
    build_run_identity,
)


def _default_data_root() -> Path:
    return Path(__file__).resolve().parents[4].parent / "data"


def _http_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
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
    app.state.run_configs: dict[str, RunCreateRequest] = {}
    app.state.inventories: dict[str, BatchInventory] = {}
    app.state.generated: dict[str, GeneratedCandidates] = {}
    app.state.work_payloads: dict[str, dict[str, Any]] = {}
    app.state.work_contexts: dict[str, Any] = {}

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
        if request.mode == "live" and not app.state.live_enabled:
            raise _http_error(
                403,
                "live_not_authorized",
                "Live provider execution requires a separate approval.",
            )
        if request.mode != "mock" or not app.state.mock_enabled:
            raise _http_error(403, "mode_not_authorized", "Mock mode is not enabled.")
        try:
            inventory = inventory_batch(
                app.state.batch_config_path,
                json_root=app.state.json_root,
                pdf_root=app.state.pdf_root,
                batch_id=request.batch_id,
                extraction_cache_root=app.state.inventory_cache_root,
            )
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
        )
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
                prompt_bundle_hash=PROMPT_BUNDLE_HASH,
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
        return augment(canonical, status="claimed", attempts=unit.attempts)

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
        return augment(
            payload,
            status="gated" if result.passed else "blocked",
            gate={
                "passed": result.passed,
                "codes": result.codes,
                "outcome": result.outcome,
            },
        )

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

    @app.post("/v1/work-units/{work_unit_id}/evidence-lock")
    def evidence_lock(work_unit_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return evidence_stage(augment(payload, work_unit_id=work_unit_id))

    @app.post("/v1/work-units/{work_unit_id}/deterministic-gate")
    def deterministic_gate(work_unit_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return gate_stage(augment(payload, work_unit_id=work_unit_id))

    @app.post("/v1/work-units/{work_unit_id}/terminal")
    def terminal(work_unit_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return terminal_stage(augment(payload, work_unit_id=work_unit_id))

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
    live_enabled=False,
    simulate_transient_failure=(
        os.environ.get("BIDMATE_EVAL_SIMULATE_TRANSIENT_FAILURE", "false").lower() == "true"
    ),
)
