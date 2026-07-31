from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LiveModelPolicy(StrictModel):
    model: str = Field(min_length=1)
    input_microusd_per_million: int = Field(gt=0)
    output_microusd_per_million: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    reasoning_effort: Literal["minimal", "low", "medium", "high"]
    price_verified_at: str = Field(min_length=1)
    price_source_url: HttpUrl


class ProviderUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class SelectedWindow(StrictModel):
    window_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class SelectorOutput(StrictModel):
    selected_windows: list[SelectedWindow] = Field(min_length=1, max_length=8)


class EvidenceClaim(StrictModel):
    window_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)


class GeneratorOutput(StrictModel):
    question: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    type: Literal["A", "B", "C", "D", "E"]
    difficulty: Literal["low", "medium", "high"]
    evidence_claims: list[EvidenceClaim] = Field(max_length=3)

    @model_validator(mode="after")
    def validate_type_specific_anchor_contract(self) -> "GeneratorOutput":
        if self.type == "D":
            if self.evidence_claims:
                raise ValueError("Type D requires zero evidence claims")
        elif not self.evidence_claims:
            raise ValueError(f"Type {self.type} requires at least one evidence claim")
        return self


class ReviewerIssue(StrictModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ReviewerOutput(StrictModel):
    decision: Literal["accept", "repair", "reject"]
    factuality: Literal["pass", "fail"]
    answerability: Literal["pass", "fail"]
    evidence_coverage: Literal["pass", "fail"]
    issues: list[ReviewerIssue]


StageName = Literal["selector", "generator", "reviewer"]
StageOutput = SelectorOutput | GeneratorOutput | ReviewerOutput


def extract_output_text(payload: dict[str, Any]) -> str:
    """Return the concatenated Responses API output_text content without retaining raw payload."""
    parts: list[str] = []
    for output in payload.get("output", []):
        if not isinstance(output, dict) or output.get("type") != "message":
            continue
        for content in output.get("content", []):
            if isinstance(content, dict) and content.get("type") == "output_text":
                parts.append(str(content.get("text", "")))
    text = "".join(parts).strip()
    if not text:
        raise ValueError("Responses API payload contains no output_text")
    return text


def parse_stage_output(stage: StageName, text: str) -> StageOutput:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("provider output is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("provider output must be a JSON object")
    model_by_stage = {
        "selector": SelectorOutput,
        "generator": GeneratorOutput,
        "reviewer": ReviewerOutput,
    }
    return model_by_stage[stage].model_validate(payload)


class ResponsesApiEnvelope(StrictModel):
    provider_response_id: str = Field(min_length=1)
    usage: ProviderUsage
    parsed_output: StageOutput

    @classmethod
    def from_provider_payload(
        cls,
        payload: dict[str, Any],
        *,
        stage: StageName = "selector",
    ) -> ResponsesApiEnvelope:
        response_id = payload.get("id")
        if not isinstance(response_id, str) or not response_id:
            raise ValueError("Responses API payload contains no response id")
        output_text = extract_output_text(payload)
        usage = ProviderUsage.model_validate(payload.get("usage", {}))
        return cls(
            provider_response_id=response_id,
            usage=usage,
            parsed_output=parse_stage_output(stage, output_text),
        )
