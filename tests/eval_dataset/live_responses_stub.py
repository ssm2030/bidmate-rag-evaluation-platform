from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from starlette.responses import StreamingResponse


@dataclass
class StubState:
    calls: list[dict[str, str]] = field(default_factory=list)
    responses_by_idempotency_key: dict[str, dict[str, object]] = field(default_factory=dict)
    scenarios: list[str] = field(default_factory=list)
    sequence: int = 0
    lock: Lock = field(default_factory=Lock)

    def record(self, *, stage: str, work_unit_id: str, idempotency_key: str) -> str:
        with self.lock:
            self.sequence += 1
            response_id = f"stub-{stage}-{self.sequence}"
            self.calls.append(
                {
                    "stage": stage,
                    "work_unit_id": work_unit_id,
                    "response_id": response_id,
                    "idempotency_key": idempotency_key,
                }
            )
            event_log = os.environ.get("BIDMATE_STUB_EVENT_LOG")
            if event_log:
                path = Path(event_log)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(self.calls, ensure_ascii=False), encoding="utf-8")
            return response_id

    def cached(self, idempotency_key: str) -> dict[str, object] | None:
        with self.lock:
            return self.responses_by_idempotency_key.get(idempotency_key)

    def cache(self, idempotency_key: str, response: dict[str, object]) -> None:
        with self.lock:
            self.responses_by_idempotency_key[idempotency_key] = response

    def set_scenarios(self, scenarios: list[str]) -> None:
        allowed = {"success", "rate_limited", "transient_server", "invalid_structured", "ambiguous", "review_repair"}
        if any(scenario not in allowed for scenario in scenarios):
            raise ValueError("invalid deterministic stub scenario")
        with self.lock:
            self.scenarios = list(scenarios)

    def next_scenario(self) -> str:
        with self.lock:
            return self.scenarios.pop(0) if self.scenarios else "success"


app = FastAPI(title="BidMate loopback Responses stub")
state = StubState()


def _payload_text(body: dict[str, Any]) -> str:
    try:
        return str(body["input"][1]["content"][0]["text"])
    except (IndexError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="missing Responses input text") from exc


def _stage_output(stage: str, text: str) -> dict[str, object]:
    payload = json.loads(text)
    windows = payload.get("windows", [])
    window = windows[0] if windows else {"window_id": "w-none", "text": "stub"}
    if stage == "selector":
        selected_windows = []
        seen_documents: set[str] = set()
        for candidate in windows:
            document_id = str(candidate.get("document_id", ""))
            if document_id in seen_documents:
                continue
            seen_documents.add(document_id)
            selected_windows.append(
                {"window_id": candidate["window_id"], "reason": "loopback"}
            )
            if len(selected_windows) == 3:
                break
        return {"selected_windows": selected_windows or [{"window_id": window["window_id"], "reason": "loopback"}]}
    if stage == "generator":
        evidence_claims = []
        claim_windows = windows[:3] if payload["sop_type"] == "B" else windows[:1]
        if payload["sop_type"] != "D":
            evidence_claims = [
                {"window_id": claim_window["window_id"], "quote": claim_window["text"]}
                for claim_window in claim_windows
            ]
        identities = list(
            dict.fromkeys(
                str(claim_window.get("text", "")).split()[0]
                for claim_window in claim_windows
                if str(claim_window.get("text", "")).split()
            )
        )
        identity_subject = " and ".join(identities) or "the supplied evidence"
        question = f"How do {identity_subject} compare?" if payload["sop_type"] == "B" else f"What does {identity_subject} say?"
        return {
            "question": question,
            "answer": "\n".join(claim_window["text"] for claim_window in claim_windows) or window["text"],
            "type": payload["sop_type"],
            "difficulty": payload["difficulty"],
            "evidence_claims": evidence_claims,
        }
    return {
        "decision": "accept",
        "factuality": "pass",
        "answerability": "pass",
        "evidence_coverage": "pass",
        "issues": [],
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ready"}


@app.get("/calls")
def calls() -> list[dict[str, str]]:
    return list(state.calls)


@app.post("/scenario-plan")
def scenario_plan(payload: dict[str, object]) -> dict[str, object]:
    scenarios = payload.get("scenarios", [])
    if not isinstance(scenarios, list) or not all(isinstance(value, str) for value in scenarios):
        raise HTTPException(status_code=400, detail="scenarios must be a list of strings")
    try:
        state.set_scenarios(scenarios)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"scenarios": list(scenarios)}

async def _ambiguous_stream():
    raise RuntimeError("deterministic loopback connection interrupted after response start")
    yield b""

@app.post("/v1/responses")
async def responses(request: Request) -> object:
    if request.headers.get("authorization") != "Bearer stub-only":
        raise HTTPException(status_code=401, detail="stub authorization required")
    idempotency_key = request.headers.get("idempotency-key")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key is required")
    body = await request.json()
    metadata = body.get("metadata", {})
    stage = metadata.get("stage")
    if stage not in {"selector", "generator", "reviewer"}:
        raise HTTPException(status_code=400, detail="invalid stage")
    work_unit_id = str(metadata.get("work_unit_id", ""))
    response_id = state.record(
        stage=stage,
        work_unit_id=work_unit_id,
        idempotency_key=idempotency_key,
    )
    cached = state.cached(idempotency_key)
    if cached is not None:
        return cached
    scenario = request.headers.get("x-bidmate-stub-scenario") or state.next_scenario()
    if scenario == "rate_limited":
        raise HTTPException(status_code=429, detail="deterministic loopback rate limit")
    if scenario == "transient_server":
        raise HTTPException(status_code=503, detail="deterministic loopback transient server failure")
    if scenario == "ambiguous":
        return StreamingResponse(_ambiguous_stream(), media_type="application/json")
    if scenario == "review_repair":
        if stage != "reviewer":
            raise HTTPException(status_code=400, detail="review_repair requires reviewer stage")
        output: object = {
            "decision": "repair",
            "factuality": "fail",
            "answerability": "pass",
            "evidence_coverage": "pass",
            "issues": [{"code": "factuality", "message": "Clarify the generated answer."}],
        }
    else:
        output = _stage_output(stage, _payload_text(body))
    if scenario == "invalid_structured":
        output = {"invalid": True}
    text = json.dumps(output, ensure_ascii=False)
    response: dict[str, object] = {
        "id": response_id,
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
    }
    state.cache(idempotency_key, response)
    return response
