from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from datetime import UTC, datetime
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

import pdfplumber

MAX_EMPTY_PAGE_RATIO = 0.20
SOURCE_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")

ALLOWED_HOSTS = {
    "www.nia.or.kr",
    "www.bok.or.kr",
    "file-cdn.bok.or.kr",
}


class InvalidPdfPayload(ValueError):
    """Raised when a download is not a PDF attachment."""


class RuntimePaths:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.pdf_root = root / "PDF1"
        self.parsed_root = root / "Parsed"
        self.manifest_path = root / "runtime-manifest.json"
        self.batch_config_path = root / "Batch_config.json"


class _AnchorCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[tuple[str, str]] = []
        self._href: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "a":
            self._href = dict(attrs).get("href")
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._href is not None:
            self.anchors.append((self._href, " ".join(self._parts).strip()))
            self._href = None
            self._parts = []


def runtime_paths(
    output_root: Path | str, *, repository_root: Path | str | None = None
) -> RuntimePaths:
    root = Path(output_root).resolve()
    repository = (
        Path(repository_root).resolve()
        if repository_root is not None
        else Path(__file__).resolve().parents[1]
    )
    expected = (repository / "artifacts" / "live_poc" / "source").resolve()
    if root != expected:
        raise ValueError(f"output root must be the repository artifact path: {expected}")
    return RuntimePaths(root)


def resolve_attachment(*, base_url: str, html: str, expected_attachment_name: str) -> str:
    parser = _AnchorCollector()
    parser.feed(html)
    expected = expected_attachment_name.casefold().strip()
    matches: set[str] = set()
    for href, label in parser.anchors:
        if expected not in label.casefold() or ".pdf" not in (href + " " + label).casefold():
            continue
        candidate = urljoin(base_url, href)
        parsed = urlparse(candidate)
        if parsed.scheme == "https":
            matches.add(candidate)
    if len(matches) != 1:
        raise ValueError("expected attachment must resolve to exactly one PDF link")
    return next(iter(matches))

def validate_pdf_payload(payload: bytes, content_type: str | None) -> None:
    normalized_type = (content_type or "").casefold().split(";", 1)[0].strip()
    accepted_types = {"application/pdf", "application/octet-stream"}
    if normalized_type not in accepted_types or not payload.startswith(b"%PDF-"):
        raise InvalidPdfPayload("download is not an accepted PDF payload with PDF magic bytes")


def runtime_manifest_record(
    *,
    source_id: str,
    organization: str,
    source_page_url: str | None,
    resolved_attachment_url: str,
    pdf_sha256: str,
    page_count: int,
    parsed_file: str,
    pdf_file: str,
    empty_page_count: int = 0,
    empty_page_ratio: float = 0.0,
    public_provenance_checked: bool = True,
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "organization": organization,
        "source_page_url": source_page_url,
        "resolved_attachment_url": resolved_attachment_url,
        "downloaded_at": datetime.now(UTC).isoformat(),
        "pdf_sha256": pdf_sha256,
        "page_count": page_count,
        "empty_page_count": empty_page_count,
        "empty_page_ratio": empty_page_ratio,
        "empty_pages_within_threshold": empty_page_ratio <= MAX_EMPTY_PAGE_RATIO,
        "parsed_file": parsed_file,
        "pdf_file": pdf_file,
        "public_provenance_checked": public_provenance_checked,
    }


def _require_https_allowed(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"URL must use an approved HTTPS host: {url}")


def load_sources(path: Path | str) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("sources"), list):
        raise ValueError("source config must have schema_version 1 and a sources array")
    sources = payload["sources"]
    if not 10 <= len(sources) <= 12:
        raise ValueError("source config must contain 10 to 12 public sources")
    source_ids: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("source entries must be objects")
        source_id = source.get("source_id")
        if not isinstance(source_id, str) or not SOURCE_ID_PATTERN.fullmatch(source_id):
            raise ValueError("source_id values must be lowercase filename-safe slugs")
        if source_id in source_ids:
            raise ValueError("source_id values must be unique")
        source_ids.add(source_id)
        url = source.get("direct_pdf_url") or source.get("source_page_url")
        if not isinstance(url, str):
            raise ValueError(f"{source_id} must provide a public source URL")
        _require_https_allowed(url)
        if not isinstance(source.get("expected_attachment_name"), str):
            raise ValueError(f"{source_id} needs expected_attachment_name")
    return sources


class _AllowlistedRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        target = urljoin(req.full_url, newurl)
        _require_https_allowed(target)
        return super().redirect_request(req, fp, code, msg, headers, target)


def _fetch(url: str) -> tuple[bytes, str | None, str]:
    _require_https_allowed(url)
    request = Request(url, headers={"User-Agent": "BidMateLiveEvalPOC/1.0"})
    opener = build_opener(_AllowlistedRedirectHandler())
    with opener.open(request, timeout=30) as response:  # noqa: S310 - every redirect is allowlisted
        final_url = response.geturl()
        _require_https_allowed(final_url)
        return response.read(), response.headers.get_content_type(), final_url


def validate_page_text_quality(pages: list[dict[str, Any]]) -> tuple[int, float]:
    if not pages or not any(str(page.get("text", "")).strip() for page in pages):
        raise ValueError("PDF text extraction produced no text")
    empty_page_count = sum(not str(page.get("text", "")).strip() for page in pages)
    empty_page_ratio = empty_page_count / len(pages)
    if empty_page_ratio > MAX_EMPTY_PAGE_RATIO:
        raise ValueError(
            f"PDF empty-page ratio {empty_page_ratio:.3f} exceeds {MAX_EMPTY_PAGE_RATIO:.3f}"
        )
    return empty_page_count, empty_page_ratio


def _inventory_stem(source_id: str) -> str:
    institution, separator, project = source_id.partition("-")
    if not separator or not institution or not project:
        raise ValueError("source_id must contain institution and project")
    return f"{institution}_{project}"


def _extract_pages(pdf_bytes: bytes) -> list[dict[str, Any]]:
    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            pages = [
                {"page_num": index, "text": page.extract_text() or ""}
                for index, page in enumerate(pdf.pages, start=1)
            ]
    except Exception as exc:
        raise InvalidPdfPayload("PDF could not be parsed") from exc
    validate_page_text_quality(pages)
    return pages


def prepare_sources(
    *,
    source_config: Path | str,
    output_root: Path | str,
    target_count: int,
    repository_root: Path | str | None = None,
) -> RuntimePaths:
    if target_count != 12:
        raise ValueError("public source preparation target_count must be exactly 12")
    sources = load_sources(source_config)
    manual_source_ids = [
        str(source["source_id"])
        for source in sources
        if source.get("requires_manual_provenance_check")
    ]
    if manual_source_ids:
        raise ValueError("manual provenance check required before download: " + ", ".join(manual_source_ids))
    paths = runtime_paths(output_root, repository_root=repository_root)
    staging = paths.root.parent / f".{paths.root.name}.staging-{uuid4().hex}"
    staging_paths = RuntimePaths(staging)
    staging_paths.pdf_root.mkdir(parents=True)
    staging_paths.parsed_root.mkdir(parents=True)
    records: list[dict[str, Any]] = []
    filenames: list[str] = []
    try:
        for source in sources:
            source_page_url = source.get("source_page_url")
            if source.get("direct_pdf_url"):
                attachment_url = str(source["direct_pdf_url"])
            else:
                page_bytes, _, final_page_url = _fetch(str(source_page_url))
                attachment_url = resolve_attachment(
                    base_url=final_page_url,
                    html=page_bytes.decode("utf-8", errors="replace"),
                    expected_attachment_name=str(source["expected_attachment_name"]),
                )
            pdf_bytes, content_type, resolved_url = _fetch(attachment_url)
            validate_pdf_payload(pdf_bytes, content_type)
            source_id = str(source["source_id"])
            pages = _extract_pages(pdf_bytes)
            empty_page_count, empty_page_ratio = validate_page_text_quality(pages)
            inventory_stem = _inventory_stem(source_id)
            filename = f"{inventory_stem}.json"
            pdf_name = f"{inventory_stem}.pdf"
            (staging_paths.pdf_root / pdf_name).write_bytes(pdf_bytes)
            (staging_paths.parsed_root / filename).write_text(
                json.dumps({"pages": pages}, ensure_ascii=False), encoding="utf-8"
            )
            filenames.append(filename)
            records.append(
                runtime_manifest_record(
                    source_id=source_id,
                    organization=str(source["organization"]),
                    source_page_url=str(source_page_url) if source_page_url else None,
                    resolved_attachment_url=resolved_url,
                    pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
                    page_count=len(pages),
                    empty_page_count=empty_page_count,
                    empty_page_ratio=empty_page_ratio,
                    parsed_file=f"Parsed/{filename}",
                    pdf_file=f"PDF1/{pdf_name}",
                    public_provenance_checked=not bool(source.get("requires_manual_provenance_check")),
                )
            )
        staging_paths.batch_config_path.write_text(
            json.dumps([{"batch_id": 1, "count": len(filenames), "files": filenames}], ensure_ascii=False),
            encoding="utf-8",
        )
        staging_paths.manifest_path.write_text(
            json.dumps({"schema_version": 1, "sources": records}, ensure_ascii=False), encoding="utf-8"
        )
        if paths.root.exists():
            raise FileExistsError("output root already exists; inspect it before replacing")
        staging.replace(paths.root)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare public PDF inputs for the local live-eval POC.")
    parser.add_argument("--source-config", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--target-count", type=int, default=12)
    args = parser.parse_args()
    paths = prepare_sources(
        source_config=args.source_config, output_root=args.output_root, target_count=args.target_count
    )
    print(json.dumps({"status": "prepared", "output_root": str(paths.root)}))


if __name__ == "__main__":
    main()
