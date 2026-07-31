from __future__ import annotations

import json
from pathlib import Path
from shutil import copytree

import pytest

from bidmate_rag.eval_dataset.automation.live_context import build_context_windows
from bidmate_rag.eval_dataset.automation.live_service import (
    LiveEvaluationService,
    UnknownEvidenceWindow,
    estimate_worst_case_microusd,
)


def _service(*, prompt_root: Path | None = None) -> LiveEvaluationService:
    return LiveEvaluationService.from_config(
        {
            "hard_cap_microusd": 5_000_000,
            "operational_cap_microusd": 4_500_000,
            "price_verified_at": "REVERIFY_BEFORE_PAID_RUN",
            "price_source_url": "https://developers.openai.com/api/docs/models",
            "stages": {
                "selector": {
                    "model": "gpt-stub-selector",
                    "input_microusd_per_million": 1_000_000,
                    "output_microusd_per_million": 6_000_000,
                    "max_output_tokens": 900,
                    "reasoning_effort": "low",
                },
                "generator": {
                    "model": "gpt-stub-generator",
                    "input_microusd_per_million": 2_500_000,
                    "output_microusd_per_million": 15_000_000,
                    "max_output_tokens": 1_400,
                    "reasoning_effort": "low",
                },
                "reviewer": {
                    "model": "gpt-stub-reviewer",
                    "input_microusd_per_million": 2_500_000,
                    "output_microusd_per_million": 15_000_000,
                    "max_output_tokens": 900,
                    "reasoning_effort": "low",
                },
            },
        },
        provider_base_url="http://127.0.0.1:8900/v1",
        stub_mode=True,
        prompt_root=prompt_root,
    )


def test_prepare_selector_uses_strict_schema_and_redacted_context() -> None:
    service = _service()
    windows = build_context_windows(
        "doc-1", [{"page_num": 1, "text": "문의 010-1234-5678 person" + "@" + "example.com"}], max_chars=600
    )

    request = service.prepare_selector(run_id="run-1", work_unit_id="unit-1", windows=windows)

    assert request.url == "http://127.0.0.1:8900/v1/responses"
    assert request.body["text"]["format"]["type"] == "json_schema"
    assert request.body["text"]["format"]["strict"] is True
    serialized = json.dumps(request.body, ensure_ascii=False)
    assert "010-1234-5678" not in serialized
    assert "person" + "@" + "example.com" not in serialized
    assert "api_key" not in serialized.lower()
    assert request.reserved_microusd > 0


def test_cost_estimate_rounds_up() -> None:
    assert estimate_worst_case_microusd(
        input_tokens=1,
        max_output_tokens=1,
        input_microusd_per_million=2_500_000,
        output_microusd_per_million=15_000_000,
    ) == 18


def test_normalize_generator_rejects_an_unknown_evidence_window() -> None:
    service = _service()
    windows = build_context_windows(
        "doc-1", [{"page_num": 1, "text": "Delivery is within 90 days."}], max_chars=600
    )
    payload = {
        "id": "resp_123",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({
            "question": "When is delivery?", "answer": "Within 90 days.", "type": "A", "difficulty": "low",
            "evidence_claims": [{"window_id": "not-in-request", "quote": "Within 90 days."}],
        })}]}],
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }

    with pytest.raises(UnknownEvidenceWindow):
        service.normalize_generator(
            provider_payload=payload,
            allowed_windows={window.window_id: window for window in windows},
        )


def test_normalize_generator_restores_a_unique_whitespace_equivalent_quote() -> None:
    service = _service()
    source_quote = "Delivery is required\nwithin 90 days."
    windows = build_context_windows(
        "doc-1", [{"page_num": 1, "text": f"Terms: {source_quote} End."}], max_chars=600
    )
    window = windows[0]
    payload = {
        "id": "resp_whitespace_quote",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            {
                                "question": "When is delivery required?",
                                "answer": "Within 90 days.",
                                "type": "A",
                                "difficulty": "low",
                                "evidence_claims": [
                                    {
                                        "window_id": window.window_id,
                                        "quote": "Delivery is required within 90 days.",
                                    }
                                ],
                            }
                        ),
                    }
                ],
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }

    normalized = service.normalize_generator(
        provider_payload=payload,
        allowed_windows={window.window_id: window},
    )

    assert normalized.output.evidence_claims[0].quote == source_quote


def test_normalize_generator_rejects_an_ambiguous_whitespace_equivalent_quote() -> None:
    service = _service()
    windows = build_context_windows(
        "doc-1",
        [
            {
                "page_num": 1,
                "text": "Delivery is\nrequired. Other clause: Delivery\tis required.",
            }
        ],
        max_chars=600,
    )
    window = windows[0]
    payload = {
        "id": "resp_ambiguous_whitespace_quote",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            {
                                "question": "What is required?",
                                "answer": "Delivery.",
                                "type": "A",
                                "difficulty": "low",
                                "evidence_claims": [
                                    {
                                        "window_id": window.window_id,
                                        "quote": "Delivery is required.",
                                    }
                                ],
                            }
                        ),
                    }
                ],
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }

    with pytest.raises(ValueError, match="exactly once"):
        service.normalize_generator(
            provider_payload=payload,
            allowed_windows={window.window_id: window},
        )


def test_live_requests_load_versioned_markdown_prompts_and_hash_the_bundle(tmp_path) -> None:
    prompt_root = tmp_path / "prompts"
    copytree(Path("prompts/eval_dataset"), prompt_root)
    service = _service(prompt_root=prompt_root)
    windows = build_context_windows(
        "doc-1", [{"page_num": 1, "text": "Contract delivery is within 90 days."}], max_chars=600
    )

    request = service.prepare_selector(run_id="run-1", work_unit_id="unit-1", windows=windows)

    selector_prompt = (prompt_root / "evidence_selector_v1.md").read_text(encoding="utf-8")
    assert request.body["input"][0]["content"][0]["text"] == selector_prompt
    assert request.body["metadata"]["prompt_bundle_hash"] == service.prompt_bundle_hash
    assert request.body["metadata"]["prompt_file_sha256"] == service.prompt_sha256("selector")
    assert "# Evidence selector v1" in request.body["input"][0]["content"][0]["text"]

    (prompt_root / "question_generator_v1.md").write_text(
        (prompt_root / "question_generator_v1.md").read_text(encoding="utf-8") + "\n# edited\n",
        encoding="utf-8",
    )
    changed = _service(prompt_root=prompt_root)
    assert changed.prompt_bundle_hash != service.prompt_bundle_hash

def test_reservation_includes_prompt_schema_and_conservative_safety_margin(tmp_path) -> None:
    prompt_root = tmp_path / "prompts"
    copytree(Path("prompts/eval_dataset"), prompt_root)
    windows = build_context_windows(
        "doc-1",
        [{"page_num": 1, "text": "Delivery requirements and evaluation criteria."}],
        max_chars=600,
    )
    short_service = _service(prompt_root=prompt_root)
    short = short_service.prepare_selector(run_id="run-1", work_unit_id="unit-1", windows=windows)
    selector_prompt = prompt_root / "evidence_selector_v1.md"
    selector_prompt.write_text(
        selector_prompt.read_text(encoding="utf-8") + "\n" + ("conservative policy context " * 400),
        encoding="utf-8",
    )
    long_service = _service(prompt_root=prompt_root)
    long = long_service.prepare_selector(run_id="run-1", work_unit_id="unit-1", windows=windows)
    payload_only = json.dumps(
        {"windows": short_service._window_payload(windows)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    payload_only_reservation = estimate_worst_case_microusd(
        input_tokens=max(1, len(payload_only.encode("utf-8")) // 3),
        max_output_tokens=short_service.policies["selector"].max_output_tokens,
        input_microusd_per_million=short_service.policies["selector"].input_microusd_per_million,
        output_microusd_per_million=short_service.policies["selector"].output_microusd_per_million,
    )

    assert short.reserved_microusd > payload_only_reservation
    assert long.reserved_microusd > short.reserved_microusd


def test_structured_repair_context_changes_request_body_only_when_requested() -> None:
    service = _service()
    windows = build_context_windows(
        "doc-1", [{"page_num": 1, "text": "Delivery is within 90 days."}], max_chars=600
    )

    original = service.prepare_selector(
        run_id="run-1", work_unit_id="unit-1", windows=windows
    )
    transport_retry = service.prepare_selector(
        run_id="run-1", work_unit_id="unit-1", windows=windows
    )
    structured_repair = service.prepare_selector(
        run_id="run-1",
        work_unit_id="unit-1",
        windows=windows,
        repair_context={"reason": "invalid_provider_response", "instruction": "Return valid JSON."},
    )

    assert transport_retry.body == original.body
    assert structured_repair.body != original.body
    repaired_input = json.loads(
        structured_repair.body["input"][1]["content"][0]["text"]
    )
    assert repaired_input["repair"]["reason"] == "invalid_provider_response"
