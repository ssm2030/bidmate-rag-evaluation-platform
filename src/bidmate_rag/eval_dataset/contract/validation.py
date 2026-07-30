"""Cross-record validations that cannot be expressed by one Pydantic model."""

from __future__ import annotations

from collections.abc import Sequence

from .models import Document, EvalItem


def validate_package_records(documents: Sequence[Document], items: Sequence[EvalItem]) -> None:
    """Ensure package identities and evidence references are internally consistent."""
    document_ids = {document.document_id for document in documents}
    document_hashes = {document.document_id: document.sha256 for document in documents}
    paths = [document.relative_pdf_path for document in documents]
    hashes = [document.sha256 for document in documents]
    if len(document_ids) != len(documents) or len(paths) != len(set(paths)):
        raise ValueError("documents must have unique ids and relative paths")
    if len(hashes) != len(set(zip(paths, hashes))):
        raise ValueError("documents must have unique path/hash identity")
    item_ids = [item.item_id for item in items]
    if len(item_ids) != len(set(item_ids)):
        raise ValueError("items must have unique ids")
    for item in items:
        for anchor in item.evidence_anchors:
            if anchor.document_id not in document_ids:
                raise ValueError("evidence anchor references an unknown document")
            if anchor.document_sha256 != document_hashes[anchor.document_id]:
                raise ValueError("evidence anchor document hash does not match its document")
