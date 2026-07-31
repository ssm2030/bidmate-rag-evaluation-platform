from __future__ import annotations

import hashlib
import json
from pathlib import Path
from shutil import copytree

import pytest
from fastapi.testclient import TestClient

from bidmate_rag.eval_dataset.automation.api import create_automation_app
from bidmate_rag.eval_dataset.contract.package_io import read_package


def _write_pdf(path: Path, text: str) -> None:
    stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    ]
    data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n".encode())
        data.extend(obj)
        data.extend(b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    data.extend(b"0000000000 65535 f \n")
    for offset in offsets:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    path.write_bytes(data)


def _client(
    tmp_path: Path,
    *,
    simulate_transient_failure: bool = False,
    live_enabled: bool = False,
    live_prompt_root: Path | None = None,
) -> tuple[TestClient, Path]:
    source_root = tmp_path / "source"
    json_root = source_root / "Parsed"
    pdf_root = source_root / "PDF1"
    json_root.mkdir(parents=True)
    pdf_root.mkdir(parents=True)
    filenames = []
    for index in range(1, 4):
        filename = f"Agency{index}_Digital service {index}.json"
        quote = f"Digital service {index} contract period is {90 + index} days and closes August {10 + index}."
        (json_root / filename).write_text(
            json.dumps({"pages": [{"page_num": 1, "text": quote}]}),
            encoding="utf-8",
        )
        _write_pdf(pdf_root / f"Agency{index}_Digital service {index}.pdf", quote)
        filenames.append(filename)
    config = tmp_path / "Batch_config.json"
    config.write_text(
        json.dumps(
            [
                {
                    "batch_id": 1,
                    "count": len(filenames),
                    "representative_domain": "Agency1_Digital service 1",
                    "files": filenames,
                }
            ]
        ),
        encoding="utf-8",
    )
    manifest_sources = []
    for filename in filenames:
        pdf_name = f"{Path(filename).stem}.pdf"
        pdf_path = pdf_root / pdf_name
        manifest_sources.append(
            {
                "source_id": Path(filename).stem,
                "parsed_file": f"Parsed/{filename}",
                "pdf_file": f"PDF1/{pdf_name}",
                "pdf_sha256": hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
                "page_count": 1,
                "public_provenance_checked": True,
                "empty_pages_within_threshold": True,
            }
        )
    (source_root / "runtime-manifest.json").write_text(
        json.dumps({"schema_version": 1, "sources": manifest_sources}), encoding="utf-8"
    )
    package_path = tmp_path / "candidate-package"
    app = create_automation_app(
        mock_enabled=True,
        live_enabled=live_enabled,
        provider_base_url="http://127.0.0.1:8900/v1",
        stub_mode=live_enabled,
        simulate_transient_failure=simulate_transient_failure,
        ledger_path=tmp_path / "ledger.sqlite3",
        package_path=package_path,
        batch_config_path=config,
        json_root=json_root,
        pdf_root=pdf_root,
        live_prompt_root=live_prompt_root,
    )
    return TestClient(app), package_path


def _run_request() -> dict[str, object]:
    return {
        "batch_id": 1,
        "target_count": 30,
        "mode": "mock",
        "max_items_per_call": 5,
    }


def test_production_app_exposes_health_and_no_testing_routes(tmp_path) -> None:
    client, _ = _client(tmp_path)
    health = client.get("/v1/health")
    assert health.status_code == 200
    assert health.json()["contract_version"] == "bidmate-eval-automation-v2"
    paths = {route.path for route in client.app.routes}
    assert not any(path.startswith("/v1/testing") for path in paths)


def test_product_api_executes_all_generator_stages_and_finalizes_package(tmp_path) -> None:
    client, package_path = _client(tmp_path)

    created = client.post("/v1/runs", json=_run_request())
    assert created.status_code == 200
    run = created.json()
    assert run["status"] == "authorized"
    assert run["resumed"] is False

    inventoried = client.post(f"/v1/runs/{run['run_id']}/inventory", json={})
    assert inventoried.status_code == 200
    assert inventoried.json()["document_count"] == 3

    planned = client.post(f"/v1/runs/{run['run_id']}/plan", json={})
    assert planned.status_code == 200
    work_units = planned.json()["work_units"]
    assert len(work_units) == 30

    for work_unit in work_units:
        payload = client.post("/v1/workflow/claim-work-unit", json=work_unit).json()
        assert payload["status"] in {"claimed", "already_done"}
        if payload["status"] == "already_done":
            continue
        payload = client.post("/v1/workflow/mock-selector", json=payload).json()
        assert payload["selector"]["document_ids"]
        payload = client.post("/v1/workflow/lock-evidence", json=payload).json()
        payload = client.post("/v1/workflow/mock-generator", json=payload).json()
        assert payload["candidate"]["question"]
        payload = client.post("/v1/workflow/deterministic-gates", json=payload).json()
        assert payload["gate"]["passed"] is True
        payload = client.post("/v1/workflow/mock-reviewer", json=payload).json()
        assert payload["review"]["critical_findings"] == []
        terminal = client.post("/v1/workflow/record-terminal-outcome", json=payload)
        assert terminal.status_code == 200
        assert terminal.json()["status"] == "done"

    finalized = client.post(f"/v1/runs/{run['run_id']}/finalize")
    assert finalized.status_code == 200
    assert finalized.json()["status"] == "completed"
    package = read_package(package_path)
    assert len(package["items"]) == 30
    assert all("Mock question" not in item["question"] for item in package["items"])


def test_same_run_identity_resumes_without_reinvoking_completed_units(tmp_path) -> None:
    client, _ = _client(tmp_path)
    first = client.post("/v1/runs", json=_run_request()).json()
    client.post(f"/v1/runs/{first['run_id']}/inventory", json={})
    work_units = client.post(f"/v1/runs/{first['run_id']}/plan", json={}).json()["work_units"]
    for unit in work_units:
        client.post("/v1/workflow/claim-work-unit", json=unit).json()
        candidate = client.app.state.generated[first["run_id"]].items[unit["ordinal"] - 1]
        client.app.state.ledger.mark_done(
            unit["work_unit_id"],
            result=candidate.model_dump(mode="json"),
        )
    client.post(f"/v1/runs/{first['run_id']}/finalize")

    resumed = client.post("/v1/runs", json=_run_request()).json()
    replay = client.post(f"/v1/runs/{resumed['run_id']}/plan", json={}).json()

    assert resumed["run_id"] == first["run_id"]
    assert resumed["resumed"] is True
    assert all(unit["status"] == "done" for unit in replay["work_units"])
    first_unit = replay["work_units"][0]
    assert (
        client.post("/v1/workflow/claim-work-unit", json=first_unit).json()["status"]
        == "already_done"
    )


def test_mock_adapter_records_one_retryable_failure_then_succeeds(tmp_path) -> None:
    client, _ = _client(tmp_path, simulate_transient_failure=True)
    run = client.post("/v1/runs", json=_run_request()).json()
    client.post(f"/v1/runs/{run['run_id']}/inventory", json={})
    unit = client.post(f"/v1/runs/{run['run_id']}/plan", json={}).json()["work_units"][0]

    payload = client.post("/v1/workflow/claim-work-unit", json=unit).json()
    payload = client.post("/v1/workflow/mock-selector", json=payload).json()
    payload = client.post("/v1/workflow/lock-evidence", json=payload).json()
    payload = client.post("/v1/workflow/mock-generator", json=payload).json()
    assert payload["adapter_failure"] == {
        "retryable": True,
        "stage": "generator",
        "message": "simulated transient local adapter failure",
    }
    payload = client.post("/v1/workflow/deterministic-gates", json=payload).json()
    assert payload["gate"]["outcome"] == "retryable_failed"
    failed = client.post("/v1/workflow/record-terminal-outcome", json=payload).json()
    assert failed["terminal_status"] == "retryable_failed"

    retryable = client.get(f"/v1/runs/{run['run_id']}/retryable").json()
    assert [entry["work_unit_id"] for entry in retryable] == [unit["work_unit_id"]]

    payload = client.post("/v1/workflow/claim-work-unit", json=retryable[0]).json()
    payload = client.post("/v1/workflow/mock-selector", json=payload).json()
    payload = client.post("/v1/workflow/lock-evidence", json=payload).json()
    payload = client.post("/v1/workflow/mock-generator", json=payload).json()
    assert payload["status"] == "generated"
    assert "adapter_failure" not in payload
    assert payload["attempts"] == 2


def test_live_run_is_fail_closed_without_separate_live_authorization(tmp_path) -> None:
    client, _ = _client(tmp_path)
    response = client.post("/v1/runs", json={**_run_request(), "mode": "live"})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "live_authorization_required"


def test_live_prepare_stage_reserves_before_returning_request(tmp_path) -> None:
    client, _ = _client(tmp_path, live_enabled=True)
    created = client.post(
        "/v1/runs",
        json={
            "batch_id": 1,
            "target_count": 5,
            "mode": "live",
            "max_items_per_call": 5,
            "campaign_key": "public-live-poc-v1",
            "data_root": "artifacts/live_poc/source",
            "cost_limit_microusd": 5_000_000,
            "live_authorized": True,
        },
    )
    assert created.status_code == 200
    planned = client.post(f"/v1/runs/{created.json()['run_id']}/plan")
    assert planned.status_code == 200
    work_unit_id = planned.json()["work_units"][0]["work_unit_id"]

    response = client.post(
        f"/v1/work-units/{work_unit_id}/selector/prepare", json={"attempt": 1}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider_call_id"]
    assert body["reserved_microusd"] > 0
    assert body["provider_request"]["url"] == "http://127.0.0.1:8900/v1/responses"


def _provider_payload(response_id: str, output: dict[str, object]) -> dict[str, object]:
    return {
        "id": response_id,
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": json.dumps(output)}],
            }
        ],
    }


def test_live_stage_normalization_reconciles_each_reserved_call(tmp_path) -> None:
    client, _ = _client(tmp_path, live_enabled=True)
    created = client.post(
        "/v1/runs",
        json={
            "batch_id": 1,
            "target_count": 5,
            "mode": "live",
            "campaign_key": "public-live-poc-v2",
            "data_root": "artifacts/live_poc/source",
            "cost_limit_microusd": 5_000_000,
            "live_authorized": True,
        },
    )
    run_id = created.json()["run_id"]
    work_unit_id = client.post(f"/v1/runs/{run_id}/plan").json()["work_units"][0]["work_unit_id"]

    selector = client.post(
        f"/v1/work-units/{work_unit_id}/selector/prepare", json={"attempt": 1}
    ).json()
    selector_input = json.loads(
        selector["provider_request"]["body"]["input"][1]["content"][0]["text"]
    )
    selected = selector_input["windows"][0]
    selector_normalized = client.post(
        f"/v1/work-units/{work_unit_id}/selector/normalize",
        json={
            "provider_call_id": selector["provider_call_id"],
            "provider_payload": _provider_payload(
                "response-selector",
                {"selected_windows": [{"window_id": selected["window_id"], "reason": "relevant"}]},
            ),
        },
    )
    assert selector_normalized.status_code == 200

    generator = client.post(
        f"/v1/work-units/{work_unit_id}/generator/prepare", json={"attempt": 1}
    ).json()
    generator_input = json.loads(
        generator["provider_request"]["body"]["input"][1]["content"][0]["text"]
    )
    window = generator_input["windows"][0]
    generator_normalized = client.post(
        f"/v1/work-units/{work_unit_id}/generator/normalize",
        json={
            "provider_call_id": generator["provider_call_id"],
            "provider_payload": _provider_payload(
                "response-generator",
                {
                    "question": "What is the contract period?",
                    "answer": window["text"],
                    "type": "A",
                    "difficulty": "low",
                    "evidence_claims": [{"window_id": window["window_id"], "quote": window["text"]}],
                },
            ),
        },
    )
    assert generator_normalized.status_code == 200

    reviewer = client.post(
        f"/v1/work-units/{work_unit_id}/reviewer/prepare", json={"attempt": 1}
    ).json()
    reviewer_normalized = client.post(
        f"/v1/work-units/{work_unit_id}/reviewer/normalize",
        json={
            "provider_call_id": reviewer["provider_call_id"],
            "provider_payload": _provider_payload(
                "response-reviewer",
                {
                    "decision": "accept",
                    "factuality": "pass",
                    "answerability": "pass",
                    "evidence_coverage": "pass",
                    "issues": [],
                },
            ),
        },
    )
    assert reviewer_normalized.status_code == 200
    costs = client.get(f"/v1/runs/{run_id}/costs")
    assert costs.status_code == 200
    assert costs.json()["actual_microusd"] > 0
    assert costs.json()["open_reserved_microusd"] == 0


def _create_live_work(
    client: TestClient, campaign_key: str, ordinal: int = 0, target_count: int = 5
) -> tuple[str, dict[str, object]]:
    created = client.post(
        "/v1/runs",
        json={
            "batch_id": 1,
            "target_count": target_count,
            "mode": "live",
            "campaign_key": campaign_key,
            "data_root": "artifacts/live_poc/source",
            "cost_limit_microusd": 5_000_000,
            "live_authorized": True,
        },
    )
    assert created.status_code == 200
    run_id = created.json()["run_id"]
    planned = client.post(f"/v1/runs/{run_id}/plan")
    assert planned.status_code == 200
    return run_id, planned.json()["work_units"][ordinal]


def test_live_selector_prepare_bounds_real_sized_context_payload(tmp_path) -> None:
    client, _ = _client(tmp_path, live_enabled=True)
    _, planned = _create_live_work(client, "public-live-poc-bounded-context", ordinal=2)
    work_unit_id = planned["work_unit_id"]
    context = client.app.state.work_contexts[work_unit_id]
    for document_index, document_id in enumerate(tuple(context.page_texts)):
        context.page_texts[document_id] = tuple(
            f"requirements scope evaluation document {document_index} page {page_number} "
            + ("bounded-evidence " * 180)
            for page_number in range(1, 13)
        )

    prepared = client.post(
        f"/v1/work-units/{work_unit_id}/selector/prepare", json={"attempt": 1}
    )

    assert prepared.status_code == 200
    selector_input = json.loads(
        prepared.json()["provider_request"]["body"]["input"][1]["content"][0]["text"]
    )
    windows = selector_input["windows"]
    assert 1 <= len(windows) <= 8
    assert sum(len(window["text"]) for window in windows) <= 12_000
    assert all(len(window["text"]) <= 1_500 for window in windows)
    assert len({window["document_id"] for window in windows}) >= 2




def test_live_prompt_bundle_hash_scopes_run_identity(tmp_path) -> None:
    prompt_root = tmp_path / "prompts"
    copytree(Path("prompts/eval_dataset"), prompt_root)
    client, _ = _client(tmp_path, live_enabled=True, live_prompt_root=prompt_root)
    request = {
        "batch_id": 1,
        "target_count": 5,
        "mode": "live",
        "campaign_key": "prompt-identity-campaign",
        "data_root": "artifacts/live_poc/source",
        "cost_limit_microusd": 5_000_000,
        "live_authorized": True,
    }
    first = client.post("/v1/runs", json=request)
    assert first.status_code == 200
    first_body = first.json()
    original_app = client.app

    generator_prompt = prompt_root / "question_generator_v1.md"
    generator_prompt.write_text(generator_prompt.read_text(encoding="utf-8") + "\n# identity edit\n", encoding="utf-8")
    reloaded = create_automation_app(
        mock_enabled=True,
        live_enabled=True,
        provider_base_url="http://127.0.0.1:8900/v1",
        stub_mode=True,
        ledger_path=original_app.state.ledger_path,
        package_path=original_app.state.package_path,
        batch_config_path=original_app.state.batch_config_path,
        json_root=original_app.state.json_root,
        pdf_root=original_app.state.pdf_root,
        live_prompt_root=prompt_root,
    )
    after_prompt_edit = TestClient(reloaded).post("/v1/runs", json=request)

    assert after_prompt_edit.status_code == 200
    assert after_prompt_edit.json()["resumed"] is False
    assert after_prompt_edit.json()["run_id"] != first_body["run_id"]
def test_live_identity_binds_campaign_key_and_normalized_data_root(tmp_path) -> None:
    client, _ = _client(tmp_path, live_enabled=True)

    def start(campaign_key: str, data_root: str) -> dict[str, object]:
        response = client.post(
            "/v1/runs",
            json={
                "batch_id": 1,
                "target_count": 5,
                "mode": "live",
                "campaign_key": campaign_key,
                "data_root": data_root,
                "cost_limit_microusd": 5_000_000,
                "live_authorized": True,
            },
        )
        assert response.status_code == 200
        return response.json()

    first = start("identity-campaign-one", "artifacts/live_poc/source")
    normalized_same_root = start("identity-campaign-one", "artifacts/live_poc/./source")
    assert normalized_same_root["resumed"] is True
    assert normalized_same_root["run_id"] == first["run_id"]

    different_campaign = start("identity-campaign-two", "artifacts/live_poc/source")
    assert different_campaign["resumed"] is False
    assert different_campaign["run_id"] != first["run_id"]

    different_root = start("identity-campaign-one", "artifacts/live_poc/alternate-source")
    assert different_root["resumed"] is False
    assert different_root["run_id"] != first["run_id"]

def test_live_untrusted_response_identity_keeps_unknown_reservation_without_retry(tmp_path) -> None:
    client, _ = _client(tmp_path, live_enabled=True)
    run_id, planned = _create_live_work(client, "public-live-poc-unknown-response")
    work_unit_id = planned["work_unit_id"]
    prepared = client.post(
        f"/v1/work-units/{work_unit_id}/selector/prepare", json={"attempt": 1}
    ).json()

    rejected = client.post(
        f"/v1/work-units/{work_unit_id}/selector/normalize",
        json={
            "provider_call_id": prepared["provider_call_id"],
            "provider_payload": {
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "not-json"}]}]
            },
        },
    )

    assert rejected.status_code == 422
    assert client.app.state.ledger.provider_call(prepared["provider_call_id"]).status == "unknown"
    assert client.get(f"/v1/runs/{run_id}/retryable").json() == []
    costs = client.get(f"/v1/runs/{run_id}/costs").json()
    assert costs["open_reserved_microusd"] == prepared["reserved_microusd"]
def _normalize_live_selector(client: TestClient, work_unit_id: str) -> dict[str, object]:
    prepared = client.post(
        f"/v1/work-units/{work_unit_id}/selector/prepare", json={"attempt": 1}
    )
    assert prepared.status_code == 200
    request = prepared.json()
    selected = json.loads(request["provider_request"]["body"]["input"][1]["content"][0]["text"])["windows"][0]
    normalized = client.post(
        f"/v1/work-units/{work_unit_id}/selector/normalize",
        json={
            "provider_call_id": request["provider_call_id"],
            "provider_payload": _provider_payload(
                "canonical-selector",
                {"selected_windows": [{"window_id": selected["window_id"], "reason": "relevant"}]},
            ),
        },
    )
    assert normalized.status_code == 200
    return normalized.json()


def test_live_normalization_preserves_canonical_payload_to_terminal_done(tmp_path) -> None:
    client, _ = _client(tmp_path, live_enabled=True)
    _, planned = _create_live_work(client, "public-live-poc-canonical")
    work_unit_id = planned["work_unit_id"]

    selected = _normalize_live_selector(client, work_unit_id)
    assert {"run_id", "work_unit_id", "mode", "sop_type", "difficulty"} <= selected.keys()
    assert selected["work_unit_id"] == work_unit_id
    assert selected["status"] == "selected"
    assert selected["attempts"] == 1

    provider_question = f"Provider question: {client.app.state.generated[planned['run_id']].items[0].question}"
    generator = client.post(
        f"/v1/work-units/{work_unit_id}/generator/prepare", json={"attempt": 1}
    ).json()
    window = json.loads(generator["provider_request"]["body"]["input"][1]["content"][0]["text"])["windows"][0]
    generated = client.post(
        f"/v1/work-units/{work_unit_id}/generator/normalize",
        json={
            "provider_call_id": generator["provider_call_id"],
            "provider_payload": _provider_payload(
                "canonical-generator",
                {
                    "question": provider_question,
                    "answer": window["text"],
                    "type": planned["sop_type"],
                    "difficulty": planned["difficulty"],
                    "evidence_claims": [{"window_id": window["window_id"], "quote": window["text"]}],
                },
            ),
        },
    )
    assert generated.status_code == 200
    generated_body = generated.json()
    assert generated_body["work_unit_id"] == work_unit_id
    assert generated_body["status"] == "generated"
    assert generated_body["candidate"]["difficulty"] == planned["difficulty"]
    assert generated_body["candidate"]["evidence_anchors"]

    gated = client.post(f"/v1/work-units/{work_unit_id}/deterministic-gate", json=generated_body)
    assert gated.status_code == 200
    gate_body = gated.json()
    assert gate_body["status"] == "gated"
    reviewer = client.post(
        f"/v1/work-units/{work_unit_id}/reviewer/prepare", json={"attempt": 1}
    ).json()
    reviewed = client.post(
        f"/v1/work-units/{work_unit_id}/reviewer/normalize",
        json={
            "provider_call_id": reviewer["provider_call_id"],
            "provider_payload": _provider_payload(
                "canonical-reviewer",
                {
                    "decision": "accept",
                    "factuality": "pass",
                    "answerability": "pass",
                    "evidence_coverage": "pass",
                    "issues": [],
                },
            ),
        },
    )
    assert reviewed.status_code == 200
    review_body = reviewed.json()
    assert review_body["candidate"] == generated_body["candidate"]
    assert review_body["gate"] == gate_body["gate"]
    assert review_body["review"]["passed"] is True
    terminal = client.post(f"/v1/work-units/{work_unit_id}/terminal", json=review_body)
    assert terminal.status_code == 200
    assert terminal.json()["terminal_status"] == "done"


def test_live_generator_uses_provider_question_and_resolved_local_evidence(tmp_path) -> None:
    client, _ = _client(tmp_path, live_enabled=True)
    run_id, planned = _create_live_work(client, "public-live-poc-canonical-evidence")
    work_unit_id = planned["work_unit_id"]
    _normalize_live_selector(client, work_unit_id)
    prepared = client.post(
        f"/v1/work-units/{work_unit_id}/generator/prepare", json={"attempt": 1}
    ).json()
    window = json.loads(prepared["provider_request"]["body"]["input"][1]["content"][0]["text"])["windows"][0]
    planned_question = client.app.state.generated[run_id].items[0].question
    provider_question = f"Provider evidence question: {planned_question}"

    normalized = client.post(
        f"/v1/work-units/{work_unit_id}/generator/normalize",
        json={
            "provider_call_id": prepared["provider_call_id"],
            "provider_payload": _provider_payload(
                "provider-generator-evidence",
                {
                    "question": provider_question,
                    "answer": window["text"],
                    "type": planned["sop_type"],
                    "difficulty": planned["difficulty"],
                    "evidence_claims": [{"window_id": window["window_id"], "quote": window["text"]}],
                },
            ),
        },
    )

    assert normalized.status_code == 200
    candidate = normalized.json()["candidate"]
    assert candidate["question"] == provider_question
    assert candidate["evidence_anchors"][0]["exact_quote"] == window["text"]
    assert candidate["evidence_anchors"][0]["resolution_status"] == "resolved"
    assert candidate["evidence_anchors"][0]["resolution_method"] == "exact"
    assert candidate["provenance"]["mode"] == "live"
    assert "mock" not in json.dumps(candidate["provenance"], sort_keys=True).casefold()


def test_live_generator_rejects_a_quote_that_is_not_unique_in_the_local_window(tmp_path) -> None:
    client, _ = _client(tmp_path, live_enabled=True)
    run_id, planned = _create_live_work(client, "public-live-poc-duplicate-quote")
    work_unit_id = planned["work_unit_id"]
    _normalize_live_selector(client, work_unit_id)
    prepared = client.post(
        f"/v1/work-units/{work_unit_id}/generator/prepare", json={"attempt": 1}
    ).json()
    window = json.loads(prepared["provider_request"]["body"]["input"][1]["content"][0]["text"])[
        "windows"
    ][0]
    assert window["text"].count("1") > 1

    rejected = client.post(
        f"/v1/work-units/{work_unit_id}/generator/normalize",
        json={
            "provider_call_id": prepared["provider_call_id"],
            "provider_payload": _provider_payload(
                "provider-generator-duplicate-quote",
                {
                    "question": f"Provider duplicate quote: {client.app.state.generated[run_id].items[0].question}",
                    "answer": "1",
                    "type": planned["sop_type"],
                    "difficulty": planned["difficulty"],
                    "evidence_claims": [{"window_id": window["window_id"], "quote": "1"}],
                },
            ),
        },
    )

    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "candidate_contract_invalid"
    assert "exactly once" in rejected.json()["detail"]["message"]



def test_live_type_d_keeps_zero_resolved_anchors_and_live_provenance(tmp_path) -> None:
    client, _ = _client(tmp_path, live_enabled=True)
    run_id, planned = _create_live_work(client, "public-live-poc-type-d", ordinal=24, target_count=30)
    assert planned["sop_type"] == "D"
    work_unit_id = planned["work_unit_id"]
    _normalize_live_selector(client, work_unit_id)
    prepared = client.post(
        f"/v1/work-units/{work_unit_id}/generator/prepare", json={"attempt": 1}
    ).json()
    provider_question = f"Provider absence question: {client.app.state.generated[run_id].items[24].question}"

    normalized = client.post(
        f"/v1/work-units/{work_unit_id}/generator/normalize",
        json={
            "provider_call_id": prepared["provider_call_id"],
            "provider_payload": _provider_payload(
                "provider-generator-type-d",
                {
                    "question": provider_question,
                    "answer": "The supplied context does not state the required term.",
                    "type": "D",
                    "difficulty": planned["difficulty"],
                    "evidence_claims": [],
                },
            ),
        },
    )

    assert normalized.status_code == 200
    candidate = normalized.json()["candidate"]
    assert candidate["question"] == provider_question
    assert candidate["evidence_anchors"] == []
    assert candidate["provenance"]["mode"] == "live"
def test_live_generator_fails_closed_on_planned_type_or_difficulty_mismatch(tmp_path) -> None:
    client, _ = _client(tmp_path, live_enabled=True)
    _, planned = _create_live_work(client, "public-live-poc-mismatch")
    work_unit_id = planned["work_unit_id"]
    _normalize_live_selector(client, work_unit_id)
    generator = client.post(
        f"/v1/work-units/{work_unit_id}/generator/prepare", json={"attempt": 1}
    ).json()
    window = json.loads(generator["provider_request"]["body"]["input"][1]["content"][0]["text"])["windows"][0]
    rejected = client.post(
        f"/v1/work-units/{work_unit_id}/generator/normalize",
        json={
            "provider_call_id": generator["provider_call_id"],
            "provider_payload": _provider_payload(
                "mismatch-generator",
                {
                    "question": "What is the contract period?",
                    "answer": window["text"],
                    "type": "B",
                    "difficulty": "high",
                    "evidence_claims": [{"window_id": window["window_id"], "quote": window["text"]}],
                },
            ),
        },
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "planned_contract_mismatch"


def test_live_provider_failure_retry_limits_and_operational_cap(tmp_path) -> None:
    client, _ = _client(tmp_path, live_enabled=True)
    run_id, first = _create_live_work(client, "public-live-poc-failures")
    planned = client.post(f"/v1/runs/{run_id}/plan").json()["work_units"]
    first_call = client.post(
        f"/v1/work-units/{first['work_unit_id']}/selector/prepare", json={"attempt": 1}
    ).json()
    definite = client.post(
        f"/v1/provider-calls/{first_call['provider_call_id']}/failure",
        json={"failure_class": "definite_rejection", "http_status": 400, "error_code": "bad_request"},
    )
    assert definite.status_code == 200
    assert definite.json()["retryable"] is False

    limited_unit = planned[1]
    limited = client.post(
        f"/v1/work-units/{limited_unit['work_unit_id']}/selector/prepare", json={"attempt": 1}
    ).json()
    first_limit = client.post(
        f"/v1/provider-calls/{limited['provider_call_id']}/failure",
        json={"failure_class": "rate_limited", "http_status": 429, "error_code": "rate_limited"},
    )
    assert first_limit.json()["retryable"] is True
    assert client.post("/v1/workflow/claim-work-unit", json=limited_unit).status_code == 200
    second = client.post(
        f"/v1/work-units/{limited_unit['work_unit_id']}/selector/prepare", json={"attempt": 2}
    ).json()
    second_limit = client.post(
        f"/v1/provider-calls/{second['provider_call_id']}/failure",
        json={"failure_class": "rate_limited", "http_status": 429, "error_code": "rate_limited"},
    )
    assert second_limit.json()["retryable"] is False

    assert limited["idempotency_key"] == second["idempotency_key"]

    server_unit = planned[3]
    server = client.post(
        f"/v1/work-units/{server_unit['work_unit_id']}/selector/prepare", json={"attempt": 1}
    ).json()
    first_server_failure = client.post(
        f"/v1/provider-calls/{server['provider_call_id']}/failure",
        json={"failure_class": "transient_server", "http_status": 503, "error_code": "server_busy"},
    )
    assert first_server_failure.status_code == 200
    assert first_server_failure.json()["retryable"] is True
    assert client.post("/v1/workflow/claim-work-unit", json=server_unit).status_code == 200
    server_retry = client.post(
        f"/v1/work-units/{server_unit['work_unit_id']}/selector/prepare", json={"attempt": 2}
    ).json()
    assert server_retry["idempotency_key"] == server["idempotency_key"]
    second_server_failure = client.post(
        f"/v1/provider-calls/{server_retry['provider_call_id']}/failure",
        json={"failure_class": "transient_server", "http_status": 503, "error_code": "server_busy"},
    )
    assert second_server_failure.status_code == 200
    assert second_server_failure.json()["retryable"] is False

    invalid_unit = planned[2]
    invalid = client.post(
        f"/v1/work-units/{invalid_unit['work_unit_id']}/selector/prepare", json={"attempt": 1}
    ).json()
    malformed = client.post(
        f"/v1/work-units/{invalid_unit['work_unit_id']}/selector/normalize",
        json={
            "provider_call_id": invalid["provider_call_id"],
            "provider_payload": {
                "id": "invalid-json",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "not-json"}]}],
            },
        },
    )
    assert malformed.status_code == 422
    replayed_invalid = client.post(
        f"/v1/provider-calls/{invalid['provider_call_id']}/failure",
        json={
            "failure_class": "invalid_response",
            "http_status": 422,
            "error_code": "invalid_provider_response",
        },
    )
    assert replayed_invalid.status_code == 200
    assert replayed_invalid.json()["retryable"] is True
    replayed_invalid_again = client.post(
        f"/v1/provider-calls/{invalid['provider_call_id']}/failure",
        json={
            "failure_class": "invalid_response",
            "http_status": 422,
            "error_code": "invalid_provider_response",
        },
    )
    assert replayed_invalid_again.status_code == 200
    assert replayed_invalid_again.json() == replayed_invalid.json()
    retryable = client.get(f"/v1/runs/{run_id}/retryable").json()
    assert [entry["work_unit_id"] for entry in retryable] == [invalid_unit["work_unit_id"]]
    assert client.post("/v1/workflow/claim-work-unit", json=invalid_unit).status_code == 200
    repaired = client.post(
        f"/v1/work-units/{invalid_unit['work_unit_id']}/selector/prepare", json={"attempt": 2}
    )
    assert repaired.status_code == 200
    assert repaired.json()["idempotency_key"] != invalid["idempotency_key"]
    repair_input = json.loads(
        repaired.json()["provider_request"]["body"]["input"][1]["content"][0]["text"]
    )
    assert repair_input["repair"]["reason"] == "invalid_provider_response"

    unknown_unit = planned[4]
    unknown_call = client.post(
        f"/v1/work-units/{unknown_unit['work_unit_id']}/selector/prepare", json={"attempt": 1}
    ).json()
    marked_unknown = client.post(
        f"/v1/provider-calls/{unknown_call['provider_call_id']}/failure",
        json={
            "failure_class": "ambiguous_transport",
            "http_status": 503,
            "error_code": "transport_unknown",
        },
    )
    assert marked_unknown.status_code == 200
    assert marked_unknown.json()["retryable"] is False
    replayed_unknown = client.post(
        f"/v1/provider-calls/{unknown_call['provider_call_id']}/failure",
        json={
            "failure_class": "invalid_response",
            "http_status": 422,
            "error_code": "invalid_provider_response",
        },
    )
    replayed_unknown_again = client.post(
        f"/v1/provider-calls/{unknown_call['provider_call_id']}/failure",
        json={
            "failure_class": "invalid_response",
            "http_status": 422,
            "error_code": "invalid_provider_response",
        },
    )
    assert replayed_unknown.status_code == 200
    assert replayed_unknown_again.status_code == 200
    assert replayed_unknown.json() == replayed_unknown_again.json() == {
        "provider_call_id": unknown_call["provider_call_id"],
        "retryable": False,
    }
    cap_unit = planned[3]
    prior = client.app.state.ledger.reserve_provider_call(
        run_id=run_id,
        work_unit_id="prior-call",
        stage="selector",
        attempt=1,
        model="stub",
        request_hash="a" * 64,
        reserved_microusd=4_500_000,
    )
    client.app.state.ledger.reconcile_provider_call(
        provider_call_id=prior.provider_call_id,
        status="succeeded",
        actual_microusd=4_500_000,
        input_tokens=1,
        output_tokens=1,
        provider_response_id="prior-response",
    )
    before_cap = client.app.state.ledger.get_cost_totals(run_id)
    capped = client.post(
        f"/v1/work-units/{cap_unit['work_unit_id']}/selector/prepare", json={"attempt": 1}
    )
    assert capped.status_code == 409
    after_cap = client.app.state.ledger.get_cost_totals(run_id)
    assert after_cap.actual_microusd == before_cap.actual_microusd
    assert after_cap.open_reserved_microusd == before_cap.open_reserved_microusd


def test_live_generator_maps_candidate_schema_failure_to_422(tmp_path) -> None:
    client, _ = _client(tmp_path, live_enabled=True)
    run_id, planned = _create_live_work(
        client,
        "public-live-poc-multi-document-contract",
        ordinal=2,
    )
    assert planned["sop_type"] == "B"
    work_unit_id = planned["work_unit_id"]
    _normalize_live_selector(client, work_unit_id)
    prepared = client.post(
        f"/v1/work-units/{work_unit_id}/generator/prepare", json={"attempt": 1}
    ).json()
    window = json.loads(
        prepared["provider_request"]["body"]["input"][1]["content"][0]["text"]
    )["windows"][0]

    rejected = client.post(
        f"/v1/work-units/{work_unit_id}/generator/normalize",
        json={
            "provider_call_id": prepared["provider_call_id"],
            "provider_payload": _provider_payload(
                "multi-document-invalid-generator",
                {
                    "question": "Compare the requirements.",
                    "answer": window["text"],
                    "type": "B",
                    "difficulty": planned["difficulty"],
                    "evidence_claims": [
                        {"window_id": window["window_id"], "quote": window["text"]}
                    ],
                },
            ),
        },
    )

    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "candidate_contract_invalid"
    assert [
        unit["work_unit_id"]
        for unit in client.get(f"/v1/runs/{run_id}/retryable").json()
    ] == [work_unit_id]


def test_live_selector_windows_put_primary_document_first_for_every_type_b(tmp_path) -> None:
    client, _ = _client(tmp_path, live_enabled=True)
    created = client.post(
        "/v1/runs",
        json={
            "batch_id": 1,
            "target_count": 30,
            "mode": "live",
            "campaign_key": "public-live-poc-primary-window-order",
            "data_root": "artifacts/live_poc/source",
            "cost_limit_microusd": 5_000_000,
            "live_authorized": True,
        },
    )
    assert created.status_code == 200
    run_id = created.json()["run_id"]
    planned = client.post(f"/v1/runs/{run_id}/plan").json()["work_units"]

    type_b_units = [unit for unit in planned if unit["sop_type"] == "B"]
    assert type_b_units
    for unit in type_b_units:
        work_unit_id = unit["work_unit_id"]
        prepared = client.post(
            f"/v1/work-units/{work_unit_id}/selector/prepare", json={"attempt": 1}
        )
        assert prepared.status_code == 200
        selector_input = json.loads(
            prepared.json()["provider_request"]["body"]["input"][1]["content"][0]["text"]
        )
        primary_document_id = str(
            client.app.state.work_contexts[work_unit_id].primary_document_id
        )
        assert selector_input["windows"][0]["document_id"] == primary_document_id



def _generate_live_candidate(
    client: TestClient,
    planned: dict[str, object],
    *,
    question: str,
    attempt: int = 1,
) -> dict[str, object]:
    work_unit_id = str(planned["work_unit_id"])
    if work_unit_id not in client.app.state.live_selected_windows:
        _normalize_live_selector(client, work_unit_id)
    prepared = client.post(
        f"/v1/work-units/{work_unit_id}/generator/prepare", json={"attempt": attempt}
    )
    assert prepared.status_code == 200
    request = prepared.json()
    window = json.loads(
        request["provider_request"]["body"]["input"][1]["content"][0]["text"]
    )["windows"][0]
    normalized = client.post(
        f"/v1/work-units/{work_unit_id}/generator/normalize",
        json={
            "provider_call_id": request["provider_call_id"],
            "provider_payload": _provider_payload(
                f"generator-{work_unit_id}-{attempt}",
                {
                    "question": question,
                    "answer": window["text"],
                    "type": planned["sop_type"],
                    "difficulty": planned["difficulty"],
                    "evidence_claims": [
                        {"window_id": window["window_id"], "quote": window["text"]}
                    ]
                    if planned["sop_type"] != "D"
                    else [],
                },
            ),
        },
    )
    return {"response": normalized, "request": request, "window": window}


def test_live_source_manifest_mutation_blocks_before_provider_reservation(tmp_path) -> None:
    client, _ = _client(tmp_path, live_enabled=True)
    run_id, planned = _create_live_work(client, "manifest-mutation")
    pdf_path = client.app.state.pdf_root / "Agency1_Digital service 1.pdf"
    pdf_path.write_bytes(pdf_path.read_bytes() + b"mutated")

    blocked = client.post(
        f"/v1/work-units/{planned['work_unit_id']}/selector/prepare", json={"attempt": 1}
    )

    assert blocked.status_code == 422
    assert blocked.json()["detail"]["code"] == "live_source_manifest_invalid"
    provider_call_count = client.app.state.ledger.connection.execute(
        "SELECT COUNT(*) FROM provider_calls WHERE run_id=?", (run_id,)
    ).fetchone()[0]
    assert provider_call_count == 0


@pytest.mark.parametrize(
    "mutation",
    [
        "non_object",
        "bool_schema_version",
        "string_provenance",
        "false_provenance",
        "string_page_quality",
        "false_page_quality",
        "string_page_count",
        "bool_page_count",
    ],
)
def test_live_source_manifest_strict_types_block_before_provider_reservation(
    tmp_path, mutation
) -> None:
    client, _ = _client(tmp_path, live_enabled=True)
    run_id, planned = _create_live_work(client, f"manifest-strict-{mutation}")
    manifest_path = client.app.state.json_root.parent / "runtime-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if mutation == "non_object":
        mutated = []
    else:
        mutated = manifest
        if mutation == "bool_schema_version":
            mutated["schema_version"] = True
        elif mutation == "string_provenance":
            mutated["sources"][0]["public_provenance_checked"] = "false"
        elif mutation == "false_provenance":
            mutated["sources"][0]["public_provenance_checked"] = False
        elif mutation == "string_page_quality":
            mutated["sources"][0]["empty_pages_within_threshold"] = "false"
        elif mutation == "false_page_quality":
            mutated["sources"][0]["empty_pages_within_threshold"] = False
        elif mutation == "string_page_count":
            mutated["sources"][0]["page_count"] = "1"
        elif mutation == "bool_page_count":
            mutated["sources"][0]["page_count"] = True
    manifest_path.write_text(json.dumps(mutated), encoding="utf-8")

    blocked = client.post(
        f"/v1/work-units/{planned['work_unit_id']}/selector/prepare", json={"attempt": 1}
    )

    assert blocked.status_code == 422
    assert blocked.json()["detail"]["code"] == "live_source_manifest_invalid"
    provider_call_count = client.app.state.ledger.connection.execute(
        "SELECT COUNT(*) FROM provider_calls WHERE run_id=?", (run_id,)
    ).fetchone()[0]
    assert provider_call_count == 0


def test_live_generator_rejects_quote_duplicated_elsewhere_on_same_page(tmp_path) -> None:
    client, _ = _client(tmp_path, live_enabled=True)
    run_id, planned = _create_live_work(client, "full-page-duplicate-quote")
    work_unit_id = str(planned["work_unit_id"])
    context = client.app.state.work_contexts[work_unit_id]
    primary = context.primary_document_id
    context.page_texts[primary] = ("UNIQUE-CLAIM " + ("x" * 1700) + " UNIQUE-CLAIM",)
    _normalize_live_selector(client, work_unit_id)
    prepared = client.post(
        f"/v1/work-units/{work_unit_id}/generator/prepare", json={"attempt": 1}
    ).json()
    supplied = json.loads(
        prepared["provider_request"]["body"]["input"][1]["content"][0]["text"]
    )["windows"][0]
    assert "UNIQUE-CLAIM" in supplied["text"]

    rejected = client.post(
        f"/v1/work-units/{work_unit_id}/generator/normalize",
        json={
            "provider_call_id": prepared["provider_call_id"],
            "provider_payload": _provider_payload(
                "duplicate-on-page",
                {
                    "question": "Where is the claim?",
                    "answer": "UNIQUE-CLAIM",
                    "type": planned["sop_type"],
                    "difficulty": planned["difficulty"],
                    "evidence_claims": [
                        {"window_id": supplied["window_id"], "quote": "UNIQUE-CLAIM"}
                    ],
                },
            ),
        },
    )

    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "candidate_contract_invalid"
    assert "source page" in rejected.json()["detail"]["message"]


def test_live_run_rejects_duplicate_candidate_question_before_review(tmp_path) -> None:
    client, _ = _client(tmp_path, live_enabled=True)
    run_id, _ = _create_live_work(client, "duplicate-candidate-run")
    planned = client.post(f"/v1/runs/{run_id}/plan").json()["work_units"]

    first = _generate_live_candidate(client, planned[0], question="What is the shared question?")
    assert first["response"].status_code == 200
    second = _generate_live_candidate(client, planned[1], question="  what  is the shared QUESTION? ")

    assert second["response"].status_code == 422
    assert second["response"].json()["detail"]["code"] == "candidate_contract_invalid"
    assert "duplicate" in second["response"].json()["detail"]["message"]


def test_live_reviewer_requests_one_generator_repair_then_blocks_second_request(tmp_path) -> None:
    client, _ = _client(tmp_path, live_enabled=True)
    _, planned = _create_live_work(client, "reviewer-generator-repair")
    work_unit_id = str(planned["work_unit_id"])
    base_question = client.app.state.generated[planned["run_id"]].items[0].question
    generated = _generate_live_candidate(
        client, planned, question=f"Provider review question: {base_question}"
    )
    assert generated["response"].status_code == 200
    gated = client.post(
        f"/v1/work-units/{work_unit_id}/deterministic-gate", json=generated["response"].json()
    ).json()
    assert gated["gate"]["passed"] is True

    reviewer = client.post(
        f"/v1/work-units/{work_unit_id}/reviewer/prepare", json={"attempt": 1}
    ).json()
    first_review = client.post(
        f"/v1/work-units/{work_unit_id}/reviewer/normalize",
        json={
            "provider_call_id": reviewer["provider_call_id"],
            "provider_payload": _provider_payload(
                "review-repair-1",
                {
                    "decision": "repair",
                    "factuality": "fail",
                    "answerability": "pass",
                    "evidence_coverage": "pass",
                    "issues": [{"code": "factuality", "message": "Clarify the answer."}],
                },
            ),
        },
    )
    assert first_review.status_code == 200
    assert first_review.json()["status"] == "repair_requested"
    assert first_review.json()["attempts"] == 2

    repair_prepared = client.post(
        f"/v1/work-units/{work_unit_id}/generator/prepare", json={"attempt": 2}
    )
    assert repair_prepared.status_code == 200
    repair_payload = json.loads(
        repair_prepared.json()["provider_request"]["body"]["input"][1]["content"][0]["text"]
    )
    assert repair_payload["repair"]["reason"] == "reviewer_requested_generator_repair"
    assert repair_payload["repair"]["issues"][0]["code"] == "factuality"
    repair_window = repair_payload["windows"][0]
    repaired = client.post(
        f"/v1/work-units/{work_unit_id}/generator/normalize",
        json={
            "provider_call_id": repair_prepared.json()["provider_call_id"],
            "provider_payload": _provider_payload(
                "generator-repair-2",
                {
                    "question": f"Clarified provider review question: {base_question}",
                    "answer": repair_window["text"],
                    "type": planned["sop_type"],
                    "difficulty": planned["difficulty"],
                    "evidence_claims": [
                        {"window_id": repair_window["window_id"], "quote": repair_window["text"]}
                    ],
                },
            ),
        },
    )
    assert repaired.status_code == 200
    client.post(f"/v1/work-units/{work_unit_id}/deterministic-gate", json=repaired.json())
    second_reviewer = client.post(
        f"/v1/work-units/{work_unit_id}/reviewer/prepare", json={"attempt": 2}
    ).json()
    assert second_reviewer["idempotency_key"] != reviewer["idempotency_key"]
    second_reviewer_input = json.loads(
        second_reviewer["provider_request"]["body"]["input"][1]["content"][0]["text"]
    )
    assert second_reviewer_input["repair"]["reason"] == "post_generator_repair_review"
    second_review = client.post(
        f"/v1/work-units/{work_unit_id}/reviewer/normalize",
        json={
            "provider_call_id": second_reviewer["provider_call_id"],
            "provider_payload": _provider_payload(
                "review-repair-2",
                {
                    "decision": "repair",
                    "factuality": "fail",
                    "answerability": "pass",
                    "evidence_coverage": "pass",
                    "issues": [{"code": "factuality", "message": "Still unclear."}],
                },
            ),
        },
    )
    assert second_review.status_code == 200
    assert second_review.json()["status"] == "blocked"
    assert second_review.json()["repair_count"] == 1
    terminal = client.post(
        f"/v1/work-units/{work_unit_id}/terminal", json=second_review.json()
    )
    assert terminal.json()["terminal_status"] == "needs_review"


def test_live_retry_claim_resumes_generator_and_reviewer_without_reissuing_prior_stage(tmp_path) -> None:
    client, _ = _client(tmp_path, live_enabled=True)
    run_id, planned = _create_live_work(client, "resume-failed-stage")
    work_unit_id = str(planned["work_unit_id"])
    _normalize_live_selector(client, work_unit_id)
    generator = client.post(
        f"/v1/work-units/{work_unit_id}/generator/prepare", json={"attempt": 1}
    ).json()
    failed_generator = client.post(
        f"/v1/provider-calls/{generator['provider_call_id']}/failure",
        json={"failure_class": "rate_limited", "http_status": 429, "error_code": "rate"},
    )
    assert failed_generator.json()["retryable"] is True
    claimed_generator = client.post("/v1/workflow/claim-work-unit", json=planned).json()
    assert claimed_generator["resume_stage"] == "generator"

    repaired = _generate_live_candidate(
        client, planned, question="What resumes at generator?", attempt=2
    )
    assert repaired["response"].status_code == 200
    client.post(
        f"/v1/work-units/{work_unit_id}/deterministic-gate", json=repaired["response"].json()
    )
    reviewer = client.post(
        f"/v1/work-units/{work_unit_id}/reviewer/prepare", json={"attempt": 2}
    ).json()
    failed_reviewer = client.post(
        f"/v1/provider-calls/{reviewer['provider_call_id']}/failure",
        json={"failure_class": "transient_server", "http_status": 503, "error_code": "server"},
    )
    assert failed_reviewer.json()["retryable"] is False

    second_run, second_planned = _create_live_work(client, "resume-reviewer-stage")
    second_work_unit = str(second_planned["work_unit_id"])
    second_generated = _generate_live_candidate(
        client, second_planned, question="What resumes at reviewer?"
    )
    assert second_generated["response"].status_code == 200
    client.post(
        f"/v1/work-units/{second_work_unit}/deterministic-gate",
        json=second_generated["response"].json(),
    )
    first_reviewer = client.post(
        f"/v1/work-units/{second_work_unit}/reviewer/prepare", json={"attempt": 1}
    ).json()
    first_failure = client.post(
        f"/v1/provider-calls/{first_reviewer['provider_call_id']}/failure",
        json={"failure_class": "transient_server", "http_status": 503, "error_code": "server"},
    )
    assert first_failure.json()["retryable"] is True
    claimed_reviewer = client.post(
        "/v1/workflow/claim-work-unit", json=second_planned
    ).json()
    assert claimed_reviewer["resume_stage"] == "reviewer"
    counts = client.app.state.ledger.connection.execute(
        "SELECT stage, COUNT(*) FROM provider_calls WHERE run_id IN (?, ?) GROUP BY stage",
        (run_id, second_run),
    ).fetchall()
    assert dict(counts)["selector"] == 2



def test_live_source_manifest_rejects_path_traversal_as_422(tmp_path) -> None:
    client, _ = _client(tmp_path, live_enabled=True)
    manifest_path = client.app.state.json_root.parent / "runtime-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sources"][0]["parsed_file"] = "../outside.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    response = client.post(
        "/v1/runs",
        json={
            "batch_id": 1,
            "target_count": 5,
            "mode": "live",
            "campaign_key": "manifest-path-traversal",
            "data_root": "artifacts/live_poc/source",
            "cost_limit_microusd": 5_000_000,
            "live_authorized": True,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "live_source_manifest_invalid"
    assert "escapes" in response.json()["detail"]["message"]



def test_operator_requeues_only_one_released_auth_failure(tmp_path) -> None:
    client, _ = _client(tmp_path, live_enabled=True)
    run_id, _ = _create_live_work(client, "public-live-poc-auth-recovery")
    planned = client.post(f"/v1/runs/{run_id}/plan").json()["work_units"]
    for unit in planned[:2]:
        prepared = client.post(
            f"/v1/work-units/{unit['work_unit_id']}/selector/prepare",
            json={"attempt": 1},
        ).json()
        failed = client.post(
            f"/v1/provider-calls/{prepared['provider_call_id']}/failure",
            json={
                "failure_class": "definite_rejection",
                "http_status": 401,
                "error_code": "selector_provider_http_401",
            },
        )
        assert failed.status_code == 200
        assert failed.json()["retryable"] is False

    recovered = client.post(
        f"/v1/runs/{run_id}/requeue-released-auth-failures",
        params={"limit": 1},
    )

    assert recovered.status_code == 200
    assert recovered.json()["requeued_count"] == 1
    assert recovered.json()["work_units"][0]["work_unit_id"] == planned[0]["work_unit_id"]
    retryable = client.get(f"/v1/runs/{run_id}/retryable").json()
    assert [unit["work_unit_id"] for unit in retryable] == [planned[0]["work_unit_id"]]


def test_operator_reconciles_captured_selector_and_resumes_generator(tmp_path) -> None:
    client, _ = _client(tmp_path, live_enabled=True)
    run_id, planned = _create_live_work(client, "public-live-poc-captured-selector")
    work_unit_id = str(planned["work_unit_id"])
    prepared = client.post(
        f"/v1/work-units/{work_unit_id}/selector/prepare",
        json={"attempt": 1},
    ).json()
    provider_call_id = prepared["provider_call_id"]
    window = client.app.state.live_prepared[provider_call_id]["windows"][0]
    client.app.state.ledger.mark_provider_call_unknown(
        provider_call_id, error_code="invalid_response"
    )
    client.app.state.ledger.record_failure(
        work_unit_id,
        error="provider_output_repair:invalid_provider_response:usage details",
        retryable=False,
        failure_stage="selector",
    )
    client.app.state.ledger.connection.execute(
        "UPDATE work_units SET attempts=2 WHERE work_unit_id=?",
        (work_unit_id,),
    )
    client.app.state.ledger.connection.commit()
    client.app.state.live_prepared.clear()
    provider_payload = {
        "id": "resp_captured_selector",
        "usage": {
            "input_tokens": 5_176,
            "output_tokens": 303,
            "total_tokens": 5_479,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 140},
        },
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            {
                                "selected_windows": [
                                    {"window_id": window.window_id, "reason": "scope"}
                                ]
                            }
                        ),
                    }
                ],
            }
        ],
    }

    recovered = client.post(
        f"/v1/work-units/{work_unit_id}/selector/reconcile-captured",
        json={"provider_call_id": provider_call_id, "provider_payload": provider_payload},
    )

    assert recovered.status_code == 200
    assert recovered.json()["status"] == "selected"
    assert recovered.json()["selector"]["window_ids"] == [window.window_id]
    retryable = client.get(f"/v1/runs/{run_id}/retryable").json()
    assert retryable[0]["failure_stage"] == "generator"
    claimed = client.post("/v1/workflow/claim-work-unit", json=planned).json()
    assert claimed["resume_stage"] == "generator"
    assert claimed["attempts"] == 3
    costs = client.get(f"/v1/runs/{run_id}/costs").json()
    assert costs["actual_microusd"] > 0
    assert costs["open_reserved_microusd"] == 0
    generator = client.post(
        f"/v1/work-units/{work_unit_id}/generator/prepare",
        json={"attempt": 3},
    )
    assert generator.status_code == 200


def test_live_attempt_three_is_rejected_without_captured_recovery(tmp_path) -> None:
    client, _ = _client(tmp_path, live_enabled=True)
    _, planned = _create_live_work(client, "public-live-poc-attempt-three-guard")
    work_unit_id = str(planned["work_unit_id"])

    response = client.post(
        f"/v1/work-units/{work_unit_id}/selector/prepare",
        json={"attempt": 3},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "captured_recovery_attempt_required"


def test_operator_replays_captured_generator_without_second_cost_and_resumes_reviewer(
    tmp_path,
) -> None:
    client, _ = _client(tmp_path, live_enabled=True)
    run_id, planned = _create_live_work(client, "public-live-poc-captured-generator")
    work_unit_id = str(planned["work_unit_id"])
    _normalize_live_selector(client, work_unit_id)
    client.app.state.ledger.connection.execute(
        "UPDATE work_units SET attempts=3, "
        "last_error='captured_provider_response_recovered', "
        "failure_stage='generator' WHERE work_unit_id=?",
        (work_unit_id,),
    )
    client.app.state.ledger.connection.commit()
    prepared = client.post(
        f"/v1/work-units/{work_unit_id}/generator/prepare",
        json={"attempt": 3},
    )
    assert prepared.status_code == 200
    request = prepared.json()
    window = client.app.state.live_prepared[request["provider_call_id"]]["windows"][0]
    source_quote = window.source_text
    provider_quote = source_quote
    provider_payload = _provider_payload(
        "resp_captured_generator",
        {
            "question": "What is the contract period?",
            "answer": provider_quote,
            "type": planned["sop_type"],
            "difficulty": planned["difficulty"],
            "evidence_claims": [
                {"window_id": window.window_id, "quote": provider_quote}
            ],
        },
    )
    service = client.app.state.live_service
    actual_microusd = service.actual_cost_microusd(
        stage="generator", input_tokens=1, output_tokens=1
    )
    client.app.state.ledger.reconcile_provider_call(
        provider_call_id=request["provider_call_id"],
        status="succeeded",
        actual_microusd=actual_microusd,
        input_tokens=1,
        output_tokens=1,
        provider_response_id="resp_captured_generator",
        error_code="invalid_response",
    )
    client.app.state.ledger.record_failure(
        work_unit_id,
        error=(
            "provider_output_repair:invalid_provider_response:"
            "evidence quote does not match the supplied outbound window"
        ),
        retryable=False,
        failure_stage="generator",
    )
    client.app.state.live_prepared.clear()
    before = client.get(f"/v1/runs/{run_id}/costs").json()

    recovered = client.post(
        f"/v1/work-units/{work_unit_id}/generator/reconcile-captured",
        json={
            "provider_call_id": request["provider_call_id"],
            "provider_payload": provider_payload,
        },
    )

    assert recovered.status_code == 200
    candidate = recovered.json()["candidate"]
    assert candidate["evidence_anchors"][0]["exact_quote"] == source_quote
    after = client.get(f"/v1/runs/{run_id}/costs").json()
    assert after["actual_microusd"] == before["actual_microusd"]
    assert after["open_reserved_microusd"] == 0
    retryable = client.get(f"/v1/runs/{run_id}/retryable").json()
    assert retryable[0]["failure_stage"] == "reviewer"
    claimed = client.post("/v1/workflow/claim-work-unit", json=retryable[0]).json()
    assert claimed["attempts"] == 3
    assert claimed["resume_stage"] == "reviewer"
    reviewer = client.post(
        f"/v1/work-units/{work_unit_id}/reviewer/prepare",
        json={"attempt": 3},
    )
    assert reviewer.status_code == 200


def test_attempt_three_multi_document_repair_restarts_selector_then_allows_generator(
    tmp_path,
) -> None:
    client, _ = _client(tmp_path, live_enabled=True)
    _, planned = _create_live_work(client, "post-auth-multi-repair", ordinal=2)
    work_unit_id = str(planned["work_unit_id"])
    client.app.state.ledger.connection.execute(
        "UPDATE work_units SET status='retryable_failed', attempts=2, "
        "last_error='provider_output_repair:candidate_contract_invalid:"
        "multi document_scope requires 2-3 unique documents', "
        "failure_stage='selector', terminal_code=NULL WHERE work_unit_id=?",
        (work_unit_id,),
    )
    client.app.state.ledger.connection.commit()
    claimed = client.post("/v1/workflow/claim-work-unit", json=planned).json()
    assert claimed["attempts"] == 3
    assert claimed["resume_stage"] == "selector"

    selector = client.post(
        f"/v1/work-units/{work_unit_id}/selector/prepare", json={"attempt": 3}
    )
    assert selector.status_code == 200
    selector_body = selector.json()
    selector_input = json.loads(
        selector_body["provider_request"]["body"]["input"][1]["content"][0]["text"]
    )
    assert "at least two unique documents" in selector_input["repair"]["instruction"]
    selected = []
    seen_documents = set()
    for window in selector_input["windows"]:
        if window["document_id"] not in seen_documents:
            seen_documents.add(window["document_id"])
            selected.append({"window_id": window["window_id"], "reason": "multi-document"})
        if len(selected) == 2:
            break
    assert len(selected) == 2
    normalized = client.post(
        f"/v1/work-units/{work_unit_id}/selector/normalize",
        json={
            "provider_call_id": selector_body["provider_call_id"],
            "provider_payload": _provider_payload(
                "selector-multi-repair",
                {"selected_windows": selected},
            ),
        },
    )
    assert normalized.status_code == 200

    generator = client.post(
        f"/v1/work-units/{work_unit_id}/generator/prepare", json={"attempt": 3}
    )
    assert generator.status_code == 200


def test_attempt_three_type_d_repair_explicitly_requires_zero_evidence_claims(
    tmp_path,
) -> None:
    client, _ = _client(tmp_path, live_enabled=True)
    _, planned = _create_live_work(client, "post-auth-type-d-repair", ordinal=4)
    work_unit_id = str(planned["work_unit_id"])
    _normalize_live_selector(client, work_unit_id)
    client.app.state.ledger.connection.execute(
        "UPDATE work_units SET status='retryable_failed', attempts=2, "
        "last_error='provider_output_repair:invalid_provider_response:"
        "Type D requires zero evidence claims', "
        "failure_stage='generator', terminal_code=NULL WHERE work_unit_id=?",
        (work_unit_id,),
    )
    client.app.state.ledger.connection.commit()
    claimed = client.post("/v1/workflow/claim-work-unit", json=planned).json()
    assert claimed["attempts"] == 3
    assert claimed["resume_stage"] == "generator"

    generator = client.post(
        f"/v1/work-units/{work_unit_id}/generator/prepare", json={"attempt": 3}
    )

    assert generator.status_code == 200
    generator_input = json.loads(
        generator.json()["provider_request"]["body"]["input"][1]["content"][0]["text"]
    )
    assert "evidence_claims must be []" in generator_input["repair"]["instruction"]
