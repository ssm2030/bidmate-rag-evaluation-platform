from __future__ import annotations

from uuid import uuid4

from bidmate_rag.eval_dataset.contract.models import BBox, EvidenceAnchor
from bidmate_rag.eval_dataset.pdf.resolver import resolve_anchor, resolve_by_bbox, resolve_manually


def _anchor(quote: str, *, sha: str = "a" * 64) -> EvidenceAnchor:
    return EvidenceAnchor(
        anchor_id=uuid4(),
        ordinal=0,
        document_id=uuid4(),
        pdf_page_number=1,
        printed_page_label=None,
        exact_quote=quote,
        context_before=None,
        context_after=None,
        role="support",
        required=True,
        resolution_status="unresolved",
        resolution_method=None,
        document_sha256=sha,
        resolver_version="v1",
        bbox=None,
    )


def test_resolver_uses_exact_then_whitespace_normalized_matching() -> None:
    exact = resolve_anchor(
        _anchor("Delivery date: 2026-08-31"), "Delivery date: 2026-08-31", "a" * 64
    )
    normalized = resolve_anchor(
        _anchor("Delivery date: 2026-08-31"), "Delivery   date:\n2026-08-31", "a" * 64
    )
    assert exact.resolution_status == "resolved" and exact.resolution_method == "exact"
    assert (
        normalized.resolution_status == "resolved"
        and normalized.resolution_method == "whitespace_normalized"
    )


def test_resolver_marks_hash_change_without_reusing_old_evidence() -> None:
    result = resolve_anchor(_anchor("Delivery date", sha="a" * 64), "Delivery date", "b" * 64)
    assert result.resolution_status == "document_changed"
    assert result.resolution_method is None


def test_synthetic_30_anchor_poc_resolves_all_fixture_quotes() -> None:
    text = "\n".join(f"Anchor {index}: verified evidence" for index in range(30))
    results = [
        resolve_anchor(_anchor(f"Anchor {index}: verified evidence"), text, "a" * 64)
        for index in range(30)
    ]
    assert all(result.resolution_status == "resolved" for result in results)


def test_resolver_context_bbox_and_manual_fallbacks_are_explicit() -> None:
    anchor = _anchor("amount").model_copy(update={"context_before": "alpha"})
    result = resolve_anchor(anchor, "alpha amount\nbeta amount", "a" * 64)
    bbox = BBox(
        x0=0.1,
        y0=0.2,
        x1=0.3,
        y1=0.4,
        coordinate_space="normalized_top_left",
        page_width=600,
        page_height=800,
        rotation=0,
    )
    assert result.resolution_method == "context_disambiguated"
    assert resolve_by_bbox(_anchor("amount"), bbox, "a" * 64).bbox == bbox
    assert resolve_manually(_anchor("amount"), bbox, "a" * 64).resolution_method == "manual"
