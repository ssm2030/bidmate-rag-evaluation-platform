"""Local-only PDF text extraction; no document content leaves this process."""

from __future__ import annotations

from pathlib import Path

import pdfplumber

from .models import ExtractedPage


def extract_pdf_pages(path: Path | str) -> list[ExtractedPage]:
    with pdfplumber.open(Path(path)) as pdf:
        pages = [
            ExtractedPage(index + 1, page.extract_text() or "")
            for index, page in enumerate(pdf.pages)
        ]
    if not pages:
        raise ValueError("PDF has no pages")
    return pages
