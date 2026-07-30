from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: int = Field(default=1, ge=1)
    target_count: int = Field(default=30, ge=1, le=100)
    mode: Literal["mock", "live"] = "mock"
    max_items_per_call: int = Field(default=5, ge=1, le=5)
