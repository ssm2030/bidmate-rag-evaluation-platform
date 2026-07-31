from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from bidmate_rag.eval_dataset.automation.live_contracts import (
    GeneratorOutput,
    LiveModelPolicy,
    ProviderUsage,
    ResponsesApiEnvelope,
    SelectorOutput,
    extract_output_text,
)


def test_canonical_live_model_prices_match_official_verification() -> None:
    payload = json.loads(Path("configs/eval_live_models.json").read_text(encoding="utf-8"))

    assert payload["price_verified_at"] == "2026-07-31"
    assert payload["price_source_url"] == "https://developers.openai.com/api/docs/models/compare"
    assert payload["stages"] == {
        "selector": {
            "model": "gpt-5.6-luna",
            "input_microusd_per_million": 1_000_000,
            "output_microusd_per_million": 6_000_000,
            "max_output_tokens": 900,
            "reasoning_effort": "low",
        },
        "generator": {
            "model": "gpt-5.6-terra",
            "input_microusd_per_million": 2_500_000,
            "output_microusd_per_million": 15_000_000,
            "max_output_tokens": 1_400,
            "reasoning_effort": "low",
        },
        "reviewer": {
            "model": "gpt-5.6-terra",
            "input_microusd_per_million": 2_500_000,
            "output_microusd_per_million": 15_000_000,
            "max_output_tokens": 900,
            "reasoning_effort": "low",
        },
    }


def test_extract_output_text_from_responses_api_content() -> None:
    payload = {
        "id": "resp_123",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": '{"selected_windows":[{"window_id":"w-1","reason":"scope"}]}',
                    }
                ],
            }
        ],
        "usage": {"input_tokens": 120, "output_tokens": 30},
    }

    assert extract_output_text(payload) == (
        '{"selected_windows":[{"window_id":"w-1","reason":"scope"}]}'
    )
    envelope = ResponsesApiEnvelope.from_provider_payload(payload)
    assert envelope.provider_response_id == "resp_123"
    assert envelope.usage == ProviderUsage(input_tokens=120, output_tokens=30)
    assert envelope.parsed_output == SelectorOutput(
        selected_windows=[{"window_id": "w-1", "reason": "scope"}]
    )


def test_usage_rejects_negative_token_counts() -> None:
    with pytest.raises(ValidationError):
        ProviderUsage(input_tokens=-1, output_tokens=0)


def test_live_model_policy_requires_positive_verified_prices() -> None:
    with pytest.raises(ValidationError):
        LiveModelPolicy(
            model="gpt-example",
            input_microusd_per_million=0,
            output_microusd_per_million=1,
            max_output_tokens=1,
            reasoning_effort="low",
            price_verified_at="2026-07-31",
            price_source_url="https://developers.openai.com/api/docs/models",
        )


def test_envelope_rejects_missing_json_model_output() -> None:
    with pytest.raises(ValueError, match="output_text"):
        ResponsesApiEnvelope.from_provider_payload({"id": "resp_123", "output": [], "usage": {}})


def test_generator_output_requires_complete_evidence_claim() -> None:
    with pytest.raises(ValidationError):
        GeneratorOutput.model_validate(
            {
                "question": "What is the delivery date?",
                "answer": "Within 90 days.",
                "type": "A",
                "difficulty": "low",
                "evidence_claims": [{"window_id": "w-1", "quote": ""}],
            }
        )


def test_generator_type_d_accepts_no_anchors_but_anchor_types_reject_them() -> None:
    type_d = GeneratorOutput.model_validate(
        {
            "question": "Which required delivery term is absent from the supplied context?",
            "answer": "The supplied context does not state a delivery term.",
            "type": "D",
            "difficulty": "low",
            "evidence_claims": [],
        }
    )

    assert type_d.evidence_claims == []
    with pytest.raises(ValidationError):
        GeneratorOutput.model_validate(
            {
                "question": "When is delivery?",
                "answer": "Within 90 days.",
                "type": "A",
                "difficulty": "low",
                "evidence_claims": [],
            }
        )


def test_provider_usage_accepts_current_responses_api_detail_fields() -> None:
    usage = ProviderUsage.model_validate(
        {
            "input_tokens": 5_176,
            "output_tokens": 303,
            "total_tokens": 5_479,
            "input_tokens_details": {"cache_write_tokens": 5_176, "cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 140},
        }
    )

    assert usage == ProviderUsage(input_tokens=5_176, output_tokens=303)
