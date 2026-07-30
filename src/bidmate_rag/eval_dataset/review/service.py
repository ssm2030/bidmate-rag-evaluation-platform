"""Service-owned evidence checks; clients never attest document hashes."""

from __future__ import annotations

import hashlib
from pathlib import Path

from bidmate_rag.eval_dataset.pdf.extractor import extract_pdf_pages
from bidmate_rag.eval_dataset.pdf.models import ExtractedPage

from .resolver_service import resolve_local_pdf_path


class ReviewEvidenceService:
    def __init__(self, pdf_root: Path | str) -> None:
        self.pdf_root = Path(pdf_root)
        self._page_cache: dict[tuple[Path, str], list[ExtractedPage]] = {}

    def document_sha256(self, relative_pdf_path: str) -> tuple[Path, str]:
        path = resolve_local_pdf_path(self.pdf_root, relative_pdf_path)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return path, digest.hexdigest()

    def verify_document(self, relative_pdf_path: str, expected_sha256: str) -> Path:
        path, digest = self.document_sha256(relative_pdf_path)
        if digest != expected_sha256:
            self._page_cache.pop((path, expected_sha256), None)
            raise ValueError("document_changed")
        return path

    def pages(
        self, relative_pdf_path: str, expected_sha256: str
    ) -> tuple[str, list[ExtractedPage]]:
        path, digest = self.document_sha256(relative_pdf_path)
        if digest != expected_sha256:
            self._page_cache.pop((path, expected_sha256), None)
        key = (path, digest)
        if key not in self._page_cache:
            self._page_cache[key] = extract_pdf_pages(path)
        return digest, self._page_cache[key]
