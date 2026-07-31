from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import tiktoken

from .live_context import ContextWindow, redact_outbound_text
from .live_contracts import (
    GeneratorOutput,
    LiveModelPolicy,
    ResponsesApiEnvelope,
    ReviewerOutput,
    SelectorOutput,
    StageName,
)


class UnknownEvidenceWindow(ValueError):
    """Raised when a provider cites evidence that was not included in its request."""


class InvalidProviderUrl(ValueError):
    """Raised when a provider request violates the paid/stub URL boundary."""


_TOKEN_ESTIMATE_SAFETY_MULTIPLIER = 1.2


def estimate_request_input_tokens(*, body: Mapping[str, Any], model: str) -> int:
    serialized = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    try:
        encoding = tiktoken.encoding_for_model(model)
        base_tokens = len(encoding.encode(serialized))
    except (KeyError, ValueError):
        base_tokens = len(serialized.encode("utf-8"))
    return max(1, math.ceil(base_tokens * _TOKEN_ESTIMATE_SAFETY_MULTIPLIER))


_PROMPT_FILENAMES: dict[StageName, str] = {
    "selector": "evidence_selector_v1.md",
    "generator": "question_generator_v1.md",
    "reviewer": "quality_reviewer_v1.md",
}


@dataclass(frozen=True)
class LivePromptBundle:
    contents: dict[StageName, str]
    versions: dict[StageName, str]
    sha256_by_stage: dict[StageName, str]
    bundle_hash: str


def load_live_prompt_bundle(prompt_root: Path | str | None = None) -> LivePromptBundle:
    root = (
        Path(prompt_root)
        if prompt_root is not None
        else Path(__file__).resolve().parents[4] / "prompts" / "eval_dataset"
    )
    contents: dict[StageName, str] = {}
    versions: dict[StageName, str] = {}
    sha256_by_stage: dict[StageName, str] = {}
    canonical_rows: list[dict[str, str]] = []
    for stage, filename in _PROMPT_FILENAMES.items():
        path = root / filename
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"required live prompt is unavailable: {path}") from exc
        if not content.strip():
            raise ValueError(f"required live prompt is empty: {path}")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        contents[stage] = content
        versions[stage] = path.stem
        sha256_by_stage[stage] = digest
        canonical_rows.append({"stage": stage, "filename": filename, "sha256": digest})
    canonical = json.dumps(canonical_rows, sort_keys=True, separators=(",", ":"))
    return LivePromptBundle(
        contents=contents,
        versions=versions,
        sha256_by_stage=sha256_by_stage,
        bundle_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )

@dataclass(frozen=True)
class PreparedProviderCall:
    stage: StageName
    url: str
    body: dict[str, Any]
    reserved_microusd: int


@dataclass(frozen=True)
class NormalizedSelectorResult:
    response_id: str
    usage_input_tokens: int
    usage_output_tokens: int
    selected_windows: tuple[ContextWindow, ...]


@dataclass(frozen=True)
class NormalizedGeneratorResult:
    response_id: str
    usage_input_tokens: int
    usage_output_tokens: int
    output: GeneratorOutput


@dataclass(frozen=True)
class NormalizedReviewerResult:
    response_id: str
    usage_input_tokens: int
    usage_output_tokens: int
    output: ReviewerOutput


def estimate_worst_case_microusd(
    *,
    input_tokens: int,
    max_output_tokens: int,
    input_microusd_per_million: int,
    output_microusd_per_million: int,
) -> int:
    """Return a conservative integer token cost rounded up to microusd."""
    if min(input_tokens, max_output_tokens, input_microusd_per_million, output_microusd_per_million) < 0:
        raise ValueError("token counts and prices must be non-negative")
    numerator = (
        input_tokens * input_microusd_per_million
        + max_output_tokens * output_microusd_per_million
    )
    return math.ceil(numerator / 1_000_000)


class LiveEvaluationService:
    OFFICIAL_BASE_URL = "https://api.openai.com/v1"

    def __init__(
        self,
        *,
        policies: Mapping[StageName, LiveModelPolicy],
        hard_cap_microusd: int,
        operational_cap_microusd: int,
        price_verified_at: str,
        provider_base_url: str = OFFICIAL_BASE_URL,
        stub_mode: bool = False,
        prompt_root: Path | str | None = None,
    ) -> None:
        if hard_cap_microusd != 5_000_000:
            raise ValueError("the live hard cap must remain 5,000,000 microusd")
        if operational_cap_microusd != 4_500_000:
            raise ValueError("the live operational cap must remain 4,500,000 microusd")
        if set(policies) != {"selector", "generator", "reviewer"}:
            raise ValueError("live model policies must define selector, generator, and reviewer")
        self.policies = dict(policies)
        self.hard_cap_microusd = hard_cap_microusd
        self.operational_cap_microusd = operational_cap_microusd
        self.price_verified_at = price_verified_at
        self.provider_base_url = self._validate_base_url(provider_base_url, stub_mode=stub_mode)
        self.stub_mode = stub_mode
        self.prompt_bundle = load_live_prompt_bundle(prompt_root)

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        *,
        provider_base_url: str = OFFICIAL_BASE_URL,
        stub_mode: bool = False,
        prompt_root: Path | str | None = None,
    ) -> LiveEvaluationService:
        price_verified_at = str(config["price_verified_at"])
        price_source_url = str(config["price_source_url"])
        raw_stages = config["stages"]
        if not isinstance(raw_stages, Mapping):
            raise ValueError("stages must be an object")
        policies: dict[StageName, LiveModelPolicy] = {}
        for stage in ("selector", "generator", "reviewer"):
            raw = raw_stages.get(stage)
            if not isinstance(raw, Mapping):
                raise ValueError(f"missing {stage} model policy")
            policies[stage] = LiveModelPolicy.model_validate(
                {**raw, "price_verified_at": price_verified_at, "price_source_url": price_source_url}
            )
        return cls(
            policies=policies,
            hard_cap_microusd=int(config["hard_cap_microusd"]),
            operational_cap_microusd=int(config["operational_cap_microusd"]),
            price_verified_at=price_verified_at,
            provider_base_url=provider_base_url,
            stub_mode=stub_mode,
            prompt_root=prompt_root,
        )

    @classmethod
    def from_config_path(
        cls,
        path: Path | str,
        *,
        provider_base_url: str = OFFICIAL_BASE_URL,
        stub_mode: bool = False,
        prompt_root: Path | str | None = None,
    ) -> LiveEvaluationService:
        return cls.from_config(
            json.loads(Path(path).read_text(encoding="utf-8")),
            provider_base_url=provider_base_url,
            stub_mode=stub_mode,
            prompt_root=prompt_root,
        )

    @classmethod
    def _validate_base_url(cls, value: str, *, stub_mode: bool) -> str:
        normalized = value.rstrip("/")
        parsed = urlparse(normalized)
        if stub_mode:
            if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
                raise InvalidProviderUrl("stub provider must use an explicit 127.0.0.1 HTTP URL")
        elif normalized != cls.OFFICIAL_BASE_URL:
            raise InvalidProviderUrl("paid provider must use the official Responses API URL")
        return normalized

    @property
    def paid_execution_ready(self) -> bool:
        return bool(self.price_verified_at) and self.price_verified_at != "REVERIFY_BEFORE_PAID_RUN"

    @staticmethod
    def _schema_for(stage: StageName) -> dict[str, Any]:
        schemas = {
            "selector": SelectorOutput,
            "generator": GeneratorOutput,
            "reviewer": ReviewerOutput,
        }
        return schemas[stage].model_json_schema()

    @property
    def prompt_bundle_hash(self) -> str:
        return self.prompt_bundle.bundle_hash

    def prompt_sha256(self, stage: StageName) -> str:
        return self.prompt_bundle.sha256_by_stage[stage]

    def _system_prompt(self, stage: StageName) -> str:
        return self.prompt_bundle.contents[stage]

    def _prepare(
        self,
        *,
        stage: StageName,
        run_id: str,
        work_unit_id: str,
        payload: Mapping[str, Any],
        repair_context: Mapping[str, Any] | None = None,
    ) -> PreparedProviderCall:
        policy = self.policies[stage]
        outbound_payload = dict(payload)
        if repair_context is not None:
            outbound_payload["repair"] = dict(repair_context)
        serialized_payload = json.dumps(
            outbound_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        body = {
            "model": policy.model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": self._system_prompt(stage)}]},
                {"role": "user", "content": [{"type": "input_text", "text": serialized_payload}]},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": f"bidmate_{stage}_v1",
                    "strict": True,
                    "schema": self._schema_for(stage),
                }
            },
            "max_output_tokens": policy.max_output_tokens,
            "reasoning": {"effort": policy.reasoning_effort},
            "metadata": {
                "run_id": run_id,
                "work_unit_id": work_unit_id,
                "stage": stage,
                "prompt_version": self.prompt_bundle.versions[stage],
                "prompt_file_sha256": self.prompt_sha256(stage),
                "prompt_bundle_hash": self.prompt_bundle_hash,
            },
        }
        estimated_input_tokens = estimate_request_input_tokens(body=body, model=policy.model)
        reserved = estimate_worst_case_microusd(
            input_tokens=estimated_input_tokens,
            max_output_tokens=policy.max_output_tokens,
            input_microusd_per_million=policy.input_microusd_per_million,
            output_microusd_per_million=policy.output_microusd_per_million,
        )
        return PreparedProviderCall(
            stage=stage,
            url=f"{self.provider_base_url}/responses",
            body=body,
            reserved_microusd=reserved,
        )

    def actual_cost_microusd(
        self, *, stage: StageName, input_tokens: int, output_tokens: int
    ) -> int:
        policy = self.policies[stage]
        return estimate_worst_case_microusd(
            input_tokens=input_tokens,
            max_output_tokens=output_tokens,
            input_microusd_per_million=policy.input_microusd_per_million,
            output_microusd_per_million=policy.output_microusd_per_million,
        )

    @staticmethod
    def _window_payload(windows: tuple[ContextWindow, ...]) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for window in windows:
            outbound_text, _ = redact_outbound_text(window.outbound_text)
            section_hint = None
            if window.section_hint:
                section_hint, _ = redact_outbound_text(window.section_hint)
            payload.append(
                {
                    "window_id": window.window_id,
                    "document_id": window.document_id,
                    "page_num": window.page_num,
                    "section_hint": section_hint,
                    "text": outbound_text,
                }
            )
        return payload

    def prepare_selector(
        self,
        *,
        run_id: str,
        work_unit_id: str,
        windows: tuple[ContextWindow, ...],
        repair_context: Mapping[str, Any] | None = None,
    ) -> PreparedProviderCall:
        if not windows:
            raise ValueError("selector requires at least one context window")
        return self._prepare(
            stage="selector",
            run_id=run_id,
            work_unit_id=work_unit_id,
            payload={"windows": self._window_payload(windows)},
            repair_context=repair_context,
        )

    def prepare_generator(
        self,
        *,
        run_id: str,
        work_unit_id: str,
        sop_type: str,
        difficulty: str,
        windows: tuple[ContextWindow, ...],
        repair_context: Mapping[str, Any] | None = None,
    ) -> PreparedProviderCall:
        return self._prepare(
            stage="generator",
            run_id=run_id,
            work_unit_id=work_unit_id,
            payload={
                "sop_type": sop_type,
                "difficulty": difficulty,
                "windows": self._window_payload(windows),
            },
            repair_context=repair_context,
        )

    def prepare_reviewer(
        self,
        *,
        run_id: str,
        work_unit_id: str,
        draft: GeneratorOutput,
        windows: tuple[ContextWindow, ...],
        repair_context: Mapping[str, Any] | None = None,
    ) -> PreparedProviderCall:
        return self._prepare(
            stage="reviewer",
            run_id=run_id,
            work_unit_id=work_unit_id,
            payload={"draft": draft.model_dump(mode="json"), "windows": self._window_payload(windows)},
            repair_context=repair_context,
        )

    @staticmethod
    def _unique_whitespace_equivalent_quote(
        text: str, quote: str, *, location: str
    ) -> str:
        if text.count(quote) >= 1:
            return quote
        tokens = quote.split()
        if not tokens:
            raise ValueError(f"evidence quote must match exactly once in the {location}")
        pattern = re.compile(r"\s+".join(re.escape(token) for token in tokens))
        matches = list(pattern.finditer(text))
        if len(matches) != 1:
            raise ValueError(f"evidence quote must match exactly once in the {location}")
        return matches[0].group(0)

    @classmethod
    def _canonicalize_allowed_claims(
        cls, output: GeneratorOutput, allowed_windows: Mapping[str, ContextWindow]
    ) -> GeneratorOutput:
        canonical_claims = []
        for claim in output.evidence_claims:
            window = allowed_windows.get(claim.window_id)
            if window is None:
                raise UnknownEvidenceWindow(f"unknown evidence window: {claim.window_id}")
            cls._unique_whitespace_equivalent_quote(
                window.outbound_text,
                claim.quote,
                location="supplied outbound window",
            )
            source_quote = cls._unique_whitespace_equivalent_quote(
                window.source_text,
                claim.quote,
                location="local source window",
            )
            canonical_claims.append(claim.model_copy(update={"quote": source_quote}))
        return output.model_copy(update={"evidence_claims": canonical_claims})

    def normalize_selector(
        self,
        *,
        provider_payload: dict[str, Any],
        allowed_windows: Mapping[str, ContextWindow],
    ) -> NormalizedSelectorResult:
        envelope = ResponsesApiEnvelope.from_provider_payload(provider_payload, stage="selector")
        assert isinstance(envelope.parsed_output, SelectorOutput)
        selected: list[ContextWindow] = []
        for selected_window in envelope.parsed_output.selected_windows:
            window = allowed_windows.get(selected_window.window_id)
            if window is None:
                raise UnknownEvidenceWindow(f"unknown evidence window: {selected_window.window_id}")
            selected.append(window)
        return NormalizedSelectorResult(
            response_id=envelope.provider_response_id,
            usage_input_tokens=envelope.usage.input_tokens,
            usage_output_tokens=envelope.usage.output_tokens,
            selected_windows=tuple(selected),
        )

    def normalize_generator(
        self,
        *,
        provider_payload: dict[str, Any],
        allowed_windows: Mapping[str, ContextWindow],
    ) -> NormalizedGeneratorResult:
        envelope = ResponsesApiEnvelope.from_provider_payload(provider_payload, stage="generator")
        assert isinstance(envelope.parsed_output, GeneratorOutput)
        output = self._canonicalize_allowed_claims(envelope.parsed_output, allowed_windows)
        return NormalizedGeneratorResult(
            response_id=envelope.provider_response_id,
            usage_input_tokens=envelope.usage.input_tokens,
            usage_output_tokens=envelope.usage.output_tokens,
            output=output,
        )

    def normalize_reviewer(self, *, provider_payload: dict[str, Any]) -> NormalizedReviewerResult:
        envelope = ResponsesApiEnvelope.from_provider_payload(provider_payload, stage="reviewer")
        assert isinstance(envelope.parsed_output, ReviewerOutput)
        return NormalizedReviewerResult(
            response_id=envelope.provider_response_id,
            usage_input_tokens=envelope.usage.input_tokens,
            usage_output_tokens=envelope.usage.output_tokens,
            output=envelope.parsed_output,
        )
