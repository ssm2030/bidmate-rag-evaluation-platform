from __future__ import annotations

import hashlib

import pytest

from bidmate_rag.eval_dataset.review.service import ReviewEvidenceService


def test_review_service_rechecks_local_document_hash(tmp_path) -> None:
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"%PDF-local")
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    service = ReviewEvidenceService(tmp_path)
    assert service.verify_document("sample.pdf", digest) == pdf
    with pytest.raises(ValueError, match="document_changed"):
        service.verify_document("sample.pdf", "a" * 64)


def test_review_service_invalidates_page_cache_when_document_hash_changes(
    tmp_path, monkeypatch
) -> None:
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"%PDF-first")
    calls: list[bytes] = []

    def fake_extract(path):
        calls.append(path.read_bytes())
        return []

    monkeypatch.setattr("bidmate_rag.eval_dataset.review.service.extract_pdf_pages", fake_extract)
    service = ReviewEvidenceService(tmp_path)
    first_hash, first_pages = service.pages("sample.pdf", "a" * 64)
    assert first_pages == []
    pdf.write_bytes(b"%PDF-second")
    second_hash, second_pages = service.pages("sample.pdf", "a" * 64)
    assert second_pages == []
    assert first_hash != second_hash
    assert calls == [b"%PDF-first", b"%PDF-second"]
