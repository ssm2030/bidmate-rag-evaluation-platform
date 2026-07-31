from __future__ import annotations

from fastapi.testclient import TestClient

from bidmate_rag.eval_dataset.automation.api import create_automation_app


def _live_request(**overrides: object) -> dict[str, object]:
    request: dict[str, object] = {
        "mode": "live",
        "campaign_key": "public-live-poc-v1",
        "data_root": "artifacts/live_poc/source",
        "target_count": 5,
        "cost_limit_microusd": 5_000_000,
        "live_authorized": False,
    }
    request.update(overrides)
    return request


def test_live_run_requires_app_enablement_and_explicit_authorization(tmp_path) -> None:
    app = create_automation_app(
        mock_enabled=True,
        live_enabled=False,
        ledger_path=tmp_path / "ledger.sqlite3",
    )

    response = TestClient(app).post("/v1/runs", json=_live_request())

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "live_authorization_required"


def test_live_run_rejects_cost_above_hard_cap_before_input_loading(tmp_path) -> None:
    app = create_automation_app(
        mock_enabled=True,
        live_enabled=True,
        ledger_path=tmp_path / "ledger.sqlite3",
        provider_base_url="http://127.0.0.1:8900/v1",
        stub_mode=True,
    )

    response = TestClient(app).post(
        "/v1/runs", json=_live_request(live_authorized=True, cost_limit_microusd=5_000_001)
    )

    assert response.status_code == 422
