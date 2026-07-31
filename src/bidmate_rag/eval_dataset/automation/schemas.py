from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: int = Field(default=1, ge=1)
    target_count: int = Field(default=30, ge=1, le=30)
    mode: Literal["mock", "live"] = "mock"
    max_items_per_call: int = Field(default=5, ge=1, le=5)
    campaign_key: str | None = None
    data_root: str | None = None
    cost_limit_microusd: int = Field(default=0, ge=0, le=5_000_000)
    live_authorized: bool = False

    @model_validator(mode="after")
    def _validate_live_contract(self) -> RunCreateRequest:
        if self.mode != "live":
            return self
        if not self.live_authorized:
            return self
        if self.target_count not in {5, 30}:
            raise ValueError("live target_count must be 5 or 30")
        if not self.campaign_key or not self.campaign_key.strip():
            raise ValueError("live runs require a campaign_key")
        if not self.data_root or not self.data_root.strip():
            raise ValueError("live runs require a data_root")
        if self.cost_limit_microusd != 5_000_000:
            raise ValueError("live cost limit must equal the 5 USD hard cap")
        return self


class PrepareStageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt: int = Field(ge=1, le=3)


class NormalizeStageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_call_id: str = Field(min_length=1)
    provider_payload: dict[str, Any]


class ProviderFailureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_class: Literal[
        "definite_rejection",
        "rate_limited",
        "transient_server",
        "ambiguous_transport",
        "invalid_response",
    ]
    http_status: int | None = Field(default=None, ge=100, le=599)
    error_code: str = Field(min_length=1)
