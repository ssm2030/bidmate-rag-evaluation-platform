from __future__ import annotations

import json
from pathlib import Path

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
) -> tuple[TestClient, Path]:
    json_root = tmp_path / "json"
    pdf_root = tmp_path / "pdf"
    json_root.mkdir()
    pdf_root.mkdir()
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
    package_path = tmp_path / "candidate-package"
    app = create_automation_app(
        mock_enabled=True,
        simulate_transient_failure=simulate_transient_failure,
        ledger_path=tmp_path / "ledger.sqlite3",
        package_path=package_path,
        batch_config_path=config,
        json_root=json_root,
        pdf_root=pdf_root,
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
    assert response.json()["detail"]["code"] == "live_not_authorized"
