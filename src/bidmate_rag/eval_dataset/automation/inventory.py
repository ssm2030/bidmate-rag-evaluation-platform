from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from bidmate_rag.eval_dataset.pdf.extractor import extract_pdf_pages
from bidmate_rag.eval_dataset.pdf.models import ExtractedPage


@dataclass(frozen=True)
class InventoryDocument:
    source_filename: str
    relative_json_path: str
    relative_pdf_path: str
    institution_name: str
    project_name: str
    source_fingerprint: str
    document_sha256: str
    page_count: int
    page_texts: tuple[str, ...]
    source_page_texts: tuple[str, ...]


@dataclass(frozen=True)
class BatchInventory:
    batch_id: int
    representative_domain: str
    documents: tuple[InventoryDocument, ...]


def _normalized_stem(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", Path(value).stem)
    return re.sub(r"\s+", " ", normalized).strip().casefold()


def _normalized_project(value: str) -> str:
    normalized = _normalized_stem(value)
    return normalized.split("_", 1)[1] if "_" in normalized else normalized


def _same_project_alias(configured: str, actual: str) -> bool:
    configured_project = _normalized_project(configured)
    actual_project = _normalized_project(actual)
    shorter = min(len(configured_project), len(actual_project))
    return shorter >= 12 and (
        configured_project.startswith(actual_project)
        or actual_project.startswith(configured_project)
    )


def _one_file(root: Path, filename: str, *, label: str) -> Path:
    direct = root / filename
    target_stem = _normalized_stem(filename)
    target_suffix = Path(filename).suffix.casefold()
    candidates = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.casefold() == target_suffix
    ]
    matches = (
        [direct]
        if direct.is_file()
        else [path for path in candidates if _normalized_stem(path.name) == target_stem]
    )
    if not matches and label == "JSON":
        matches = [path for path in candidates if _same_project_alias(filename, path.name)]
    if len(matches) != 1:
        raise ValueError(
            f"{label} mapping requires exactly one file for {filename}; found {len(matches)}"
        )
    return matches[0]


def _one_pdf(root: Path, json_filename: str) -> Path:
    expected = f"{Path(json_filename).stem}.pdf"
    direct = root / expected
    if direct.is_file():
        return direct
    target = _normalized_stem(expected)
    matches = [path for path in root.rglob("*.pdf") if _normalized_stem(path.name) == target]
    if len(matches) != 1:
        raise ValueError(
            f"PDF mapping requires exactly one file for {json_filename}; found {len(matches)}"
        )
    return matches[0]


def _identity(filename: str) -> tuple[str, str]:
    stem = Path(filename).stem.strip()
    if "_" not in stem:
        raise ValueError(f"source filename must contain institution and project: {filename}")
    institution, project = stem.split("_", 1)
    if not institution.strip() or not project.strip():
        raise ValueError(f"source filename identity is incomplete: {filename}")
    return institution.strip(), project.strip()


def _all_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _all_strings(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _all_strings(item)]
    return []


def _source_pages(payload: Any) -> tuple[str, ...]:
    if isinstance(payload, dict) and isinstance(payload.get("pages"), list):
        pages = payload["pages"]
        if pages and all(isinstance(page, dict) for page in pages):
            ordered = sorted(
                pages,
                key=lambda page: int(page.get("page_num", len(pages) + 1)),
            )
            texts = tuple(
                str(page.get("text", "")) for page in ordered if str(page.get("text", "")).strip()
            )
            if texts:
                return texts
    texts = tuple(text for text in _all_strings(payload) if text.strip())
    if not texts:
        raise ValueError("selected JSON has no source text")
    return texts


EXTRACTION_CACHE_VERSION = "pdfplumber-v1"


def _cached_pdf_pages(
    pdf_path: Path,
    document_sha256: str,
    cache_root: Path | None,
) -> list[ExtractedPage]:
    if cache_root is None:
        return extract_pdf_pages(pdf_path)
    version_root = cache_root / EXTRACTION_CACHE_VERSION
    cache_path = version_root / f"{document_sha256}.json"
    if cache_path.is_file():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            pages = payload["pages"]
            if (
                payload.get("version") == EXTRACTION_CACHE_VERSION
                and payload.get("document_sha256") == document_sha256
                and isinstance(pages, list)
                and pages
                and all(
                    isinstance(page, dict)
                    and page.get("page_number") == index
                    and isinstance(page.get("text"), str)
                    for index, page in enumerate(pages, start=1)
                )
            ):
                return [ExtractedPage(page["page_number"], page["text"]) for page in pages]
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            pass
    pages = extract_pdf_pages(pdf_path)
    version_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "document_sha256": document_sha256,
        "pages": [{"page_number": page.page_number, "text": page.text} for page in pages],
        "version": EXTRACTION_CACHE_VERSION,
    }
    temporary = version_root / f".{document_sha256}.{uuid4().hex}.tmp"
    temporary.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    temporary.replace(cache_path)
    return pages


def inventory_batch(
    batch_config_path: Path | str,
    *,
    json_root: Path | str,
    pdf_root: Path | str,
    batch_id: int,
    extraction_cache_root: Path | str | None = None,
) -> BatchInventory:
    config_path = Path(batch_config_path)
    json_base = Path(json_root).resolve()
    pdf_base = Path(pdf_root).resolve()
    cache_root = None if extraction_cache_root is None else Path(extraction_cache_root).resolve()
    rows = json.loads(config_path.read_text(encoding="utf-8-sig"))
    selected = [row for row in rows if int(row["batch_id"]) == int(batch_id)]
    if len(selected) != 1:
        raise ValueError(f"batch_id {batch_id} must appear exactly once")
    row = selected[0]
    filenames = list(row.get("files", []))
    if int(row.get("count", len(filenames))) != len(filenames) or not filenames:
        raise ValueError("Batch file count is invalid")
    documents: list[InventoryDocument] = []
    for filename in filenames:
        json_path = _one_file(json_base, filename, label="JSON")
        pdf_path = _one_pdf(pdf_base, json_path.name)
        institution, project = _identity(json_path.name)
        json_bytes = json_path.read_bytes()
        pdf_bytes = pdf_path.read_bytes()
        document_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
        source_payload = json.loads(json_bytes.decode("utf-8-sig"))
        source_page_texts = _source_pages(source_payload)
        pages = _cached_pdf_pages(pdf_path, document_sha256, cache_root)
        if not any(page.text.strip() for page in pages):
            raise ValueError(f"PDF text extraction produced no text: {pdf_path.name}")
        documents.append(
            InventoryDocument(
                source_filename=json_path.name,
                relative_json_path=json_path.relative_to(json_base).as_posix(),
                relative_pdf_path=pdf_path.relative_to(pdf_base).as_posix(),
                institution_name=institution,
                project_name=project,
                source_fingerprint=hashlib.sha256(json_bytes).hexdigest(),
                document_sha256=document_sha256,
                page_count=len(pages),
                page_texts=tuple(page.text for page in pages),
                source_page_texts=source_page_texts,
            )
        )
    return BatchInventory(
        int(batch_id),
        str(row.get("representative_domain", "")),
        tuple(documents),
    )


def inventory(pdf_root: Path) -> list[Path]:
    return sorted(pdf_root.rglob("*.pdf"))
