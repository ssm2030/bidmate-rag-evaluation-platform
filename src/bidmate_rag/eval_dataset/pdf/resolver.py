"""Evidence resolver: exact matching before explicit fallback paths."""

from __future__ import annotations

from bidmate_rag.eval_dataset.contract.models import BBox, EvidenceAnchor

from .normalizer import normalize_whitespace


def _changed(anchor: EvidenceAnchor, current_document_sha256: str) -> EvidenceAnchor | None:
    if anchor.document_sha256 != current_document_sha256:
        return anchor.model_copy(
            update={"resolution_status": "document_changed", "resolution_method": None}
        )
    return None


def resolve_anchor(
    anchor: EvidenceAnchor, page_text: str, current_document_sha256: str
) -> EvidenceAnchor:
    """Resolve by exact, then whitespace, then explicit context disambiguation."""
    if changed := _changed(anchor, current_document_sha256):
        return changed
    exact_count = page_text.count(anchor.exact_quote)
    if exact_count == 1:
        return anchor.model_copy(
            update={"resolution_status": "resolved", "resolution_method": "exact"}
        )
    normalized_quote = normalize_whitespace(anchor.exact_quote)
    normalized_page = normalize_whitespace(page_text)
    normalized_count = normalized_page.count(normalized_quote)
    if normalized_count == 1:
        return anchor.model_copy(
            update={"resolution_status": "resolved", "resolution_method": "whitespace_normalized"}
        )
    if anchor.context_before:
        contextual = normalize_whitespace(f"{anchor.context_before} {anchor.exact_quote}")
        if normalized_page.count(contextual) == 1:
            return anchor.model_copy(
                update={
                    "resolution_status": "resolved",
                    "resolution_method": "context_disambiguated",
                }
            )
    status = "ambiguous" if exact_count > 1 or normalized_count > 1 else "unresolved"
    return anchor.model_copy(update={"resolution_status": status, "resolution_method": None})


def resolve_by_bbox(
    anchor: EvidenceAnchor, bbox: BBox, current_document_sha256: str
) -> EvidenceAnchor:
    """Attach a deterministic PDF-coordinate fallback after local resolver evidence."""
    if changed := _changed(anchor, current_document_sha256):
        return changed
    return anchor.model_copy(
        update={"resolution_status": "resolved", "resolution_method": "bbox", "bbox": bbox}
    )


def resolve_manually(
    anchor: EvidenceAnchor, bbox: BBox, current_document_sha256: str
) -> EvidenceAnchor:
    """Record a local human-selected box; it cannot override a changed document hash."""
    if changed := _changed(anchor, current_document_sha256):
        return changed
    return anchor.model_copy(
        update={"resolution_status": "resolved", "resolution_method": "manual", "bbox": bbox}
    )
