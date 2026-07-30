"""Pydantic models for the immutable Schema v2 package boundary."""

from __future__ import annotations

import re
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Document(StrictModel):
    document_id: UUID
    relative_pdf_path: str = Field(min_length=1)
    sha256: str
    page_count: int = Field(ge=1)
    legacy_filename: str = Field(min_length=1)
    external_ids: dict[str, str]
    source_classification: Literal["public", "private", "unknown"]
    external_transmission_allowed: bool

    @model_validator(mode="after")
    def validate_path_and_hash(self) -> "Document":
        parts = self.relative_pdf_path.split("/")
        if (
            "\\" in self.relative_pdf_path
            or self.relative_pdf_path.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)
            or re.match(r"^[A-Za-z]:", self.relative_pdf_path)
        ):
            raise ValueError("relative_pdf_path must be a safe POSIX relative path")
        if not SHA256_RE.fullmatch(self.sha256):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        return self


class BBox(StrictModel):
    x0: float = Field(ge=0, le=1)
    y0: float = Field(ge=0, le=1)
    x1: float = Field(ge=0, le=1)
    y1: float = Field(ge=0, le=1)
    coordinate_space: Literal["normalized_top_left"]
    page_width: float = Field(gt=0)
    page_height: float = Field(gt=0)
    rotation: int

    @model_validator(mode="after")
    def validate_order(self) -> "BBox":
        if self.x0 >= self.x1 or self.y0 >= self.y1:
            raise ValueError("bbox bounds must be ordered")
        return self


class EvidenceAnchor(StrictModel):
    anchor_id: UUID
    ordinal: int = Field(ge=0)
    document_id: UUID
    pdf_page_number: int = Field(ge=1)
    printed_page_label: str | None
    exact_quote: str = Field(min_length=1)
    context_before: str | None
    context_after: str | None
    role: Literal["support", "contradiction"]
    required: bool
    resolution_status: Literal["resolved", "unresolved", "ambiguous", "document_changed"]
    resolution_method: (
        Literal["exact", "whitespace_normalized", "context_disambiguated", "bbox", "manual"] | None
    )
    document_sha256: str
    resolver_version: str = Field(min_length=1)
    bbox: BBox | None

    @model_validator(mode="after")
    def validate_hash(self) -> "EvidenceAnchor":
        if not SHA256_RE.fullmatch(self.document_sha256):
            raise ValueError("document_sha256 must be a lowercase SHA-256 digest")
        if self.resolution_status == "resolved" and self.resolution_method is None:
            raise ValueError("resolved anchor requires a resolution_method")
        return self


class EvalItem(StrictModel):
    item_id: UUID
    revision: int = Field(ge=1)
    status: Literal["draft", "needs_anchor_fix", "needs_review", "approved", "rejected"]
    question: str = Field(min_length=1)
    ground_truth_answer: str = Field(min_length=1)
    task_kind: Literal["extract", "compare", "summarize", "calculate", "follow_up"]
    document_scope: Literal["single", "multi"]
    answerability: Literal["answerable", "contradiction", "unanswerable"]
    evidence_mode: Literal["direct_quote", "table", "multi_evidence", "none"]
    perturbation: Literal["none", "typo", "abbreviation", "fragmented"]
    difficulty: Literal["low", "medium", "high"] = "medium"
    metadata_filter: dict[str, Any]
    history: list[dict[str, str]]
    verification_notes: list[str] = Field(max_length=5)
    provenance: dict[str, Any]
    evidence_anchors: list[EvidenceAnchor] = Field(max_length=3)

    @model_validator(mode="after")
    def validate_evidence_contract(self) -> "EvalItem":
        anchors = self.evidence_anchors
        if self.answerability == "unanswerable":
            if anchors or self.evidence_mode != "none":
                raise ValueError("unanswerable items require zero anchors and evidence_mode=none")
            return self
        if not 1 <= len(anchors) <= 3:
            raise ValueError("answerable or contradiction items require 1-3 anchors")
        if self.status == "approved" and any(
            anchor.required and anchor.resolution_status != "resolved" for anchor in anchors
        ):
            raise ValueError("approved items require resolved required evidence anchors")
        if self.answerability == "contradiction" and not any(
            anchor.role == "contradiction" for anchor in anchors
        ):
            raise ValueError("contradiction items require a contradiction anchor")
        document_count = len({anchor.document_id for anchor in anchors})
        if self.document_scope == "single" and document_count != 1:
            raise ValueError("single document_scope requires one unique document")
        if self.document_scope == "multi" and not 2 <= document_count <= 3:
            raise ValueError("multi document_scope requires 2-3 unique documents")
        if any(len(note) > 240 for note in self.verification_notes):
            raise ValueError("verification_notes entries must be <= 240 characters")
        return self
