"""Stable hashes for source identity and package integrity."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from .models import Document


def source_set_hash(documents: Iterable[Document]) -> str:
    rows = sorted(documents, key=lambda document: str(document.document_id))
    payload = b"".join(
        f"{document.document_id}\0{document.sha256}\0{document.relative_pdf_path}\n".encode("utf-8")
        for document in rows
    )
    return hashlib.sha256(payload).hexdigest()
