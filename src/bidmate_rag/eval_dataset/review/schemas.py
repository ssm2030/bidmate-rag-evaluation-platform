"""FastAPI request envelopes for the local-only review workstation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DraftRequest(StrictRequest):
    base_revision: int = Field(ge=1)
    patch: dict[str, Any]


class ResolveAnchorRequest(StrictRequest):
    base_revision: int = Field(ge=1)
    method: Literal["bbox", "manual"]
    bbox: dict[str, Any]
    selected_quote: str = Field(min_length=1)
    page_number: int = Field(ge=1)


class DecisionRequest(StrictRequest):
    base_revision: int = Field(ge=1)
    reason: str = ""


class ResumeRequest(StrictRequest):
    item_id: str = Field(min_length=1)
    anchor_id: str | None = None
