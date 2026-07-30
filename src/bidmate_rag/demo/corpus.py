"""Create a deterministic synthetic corpus without shipping source documents."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


@dataclass(frozen=True)
class DemoDocument:
    """One generated document in the public demo corpus."""

    doc_id: str
    institution: str
    project: str
    json_path: Path
    pdf_path: Path
    text: str


@dataclass(frozen=True)
class DemoCorpus:
    """Paths and integrity metadata for a generated public demo corpus."""

    output_root: Path
    source_root: Path
    json_root: Path
    pdf_root: Path
    batch_config: Path
    chunks_path: Path
    metadata_path: Path
    documents: tuple[DemoDocument, ...]
    source_set_sha256: str


def _pdf_bytes(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 7 Tf 54 720 Td ({escaped}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        (
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        ),
    ]
    data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for index, item in enumerate(objects, start=1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n".encode("ascii"))
        data.extend(item)
        data.extend(b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    data.extend(b"0000000000 65535 f \n")
    for offset in offsets:
        data.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    data.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(data)


def _source_set_hash(source_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in source_root.rglob("*") if item.is_file()):
        relative = path.relative_to(source_root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        payload = path.read_bytes()
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def create_public_demo_corpus(
    output_root: Path | str,
    *,
    document_count: int = 15,
) -> DemoCorpus:
    """Generate a deterministic, fictional corpus for RAG and review demos."""

    if not 1 <= document_count <= 98:
        raise ValueError("document_count must be between 1 and 98")

    root = Path(output_root).resolve()
    source_root = root / "source"
    json_root = source_root / "Parsed"
    pdf_root = source_root / "PDF1"
    rag_root = root / "rag"
    json_root.mkdir(parents=True, exist_ok=True)
    pdf_root.mkdir(parents=True, exist_ok=True)
    rag_root.mkdir(parents=True, exist_ok=True)

    documents: list[DemoDocument] = []
    chunk_rows: list[dict[str, object]] = []
    metadata_rows: list[dict[str, object]] = []
    filenames: list[str] = []

    for index in range(1, document_count + 1):
        institution = f"DemoAgency{index:02d}"
        project = f"DemoProject{index:02d}"
        doc_id = f"demo-{index:02d}"
        stem = f"{institution}_{project}"
        filename = f"{stem}.json"
        budget = 100 + index
        duration = 120 + index
        text = (
            f"{project} budget is {budget} million won. "
            f"Delivery period is {duration} days. "
            "Encryption and audit logging are required. "
            "Evaluation weights are technical 80 and price 20."
        )
        json_path = json_root / filename
        pdf_path = pdf_root / f"{stem}.pdf"
        json_path.write_text(
            json.dumps(
                {"pages": [{"page_num": 1, "text": text}]},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        pdf_path.write_bytes(_pdf_bytes(text))
        filenames.append(filename)
        documents.append(
            DemoDocument(
                doc_id=doc_id,
                institution=institution,
                project=project,
                json_path=json_path,
                pdf_path=pdf_path,
                text=text,
            )
        )
        chunk_rows.append(
            {
                "chunk_id": f"{doc_id}-chunk-000",
                "doc_id": doc_id,
                "text": text,
                "text_with_meta": f"{institution} {project} requirements. {text}",
                "char_count": len(text),
                "section": "requirements",
                "content_type": "text",
                "chunk_index": 0,
                "organization": institution,
                "project_name": project,
                "filename": filename,
                "파일명": filename,
                "사업명": project,
                "발주 기관": institution,
            }
        )
        metadata_rows.append(
            {
                "doc_id": doc_id,
                "organization": institution,
                "project_name": project,
                "filename": filename,
                "summary": text,
                "text": text,
                "파일명": filename,
                "사업명": project,
                "발주 기관": institution,
                "사업 금액": budget * 1_000_000,
                "본문_정제": text,
                "본문_마크다운": text,
                "ingest_file": filename,
                "canonical_file": filename,
                "ingest_enabled": True,
            }
        )

    batch_config = source_root / "Batch_config.json"
    batch_config.write_text(
        json.dumps(
            [
                {
                    "batch_id": 1,
                    "count": len(filenames),
                    "representative_domain": "synthetic-public-demo",
                    "files": filenames,
                }
            ],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    chunks_path = rag_root / "chunks.parquet"
    metadata_path = rag_root / "metadata.parquet"
    pd.DataFrame(chunk_rows).to_parquet(chunks_path, index=False)
    pd.DataFrame(metadata_rows).to_parquet(metadata_path, index=False)

    return DemoCorpus(
        output_root=root,
        source_root=source_root,
        json_root=json_root,
        pdf_root=pdf_root,
        batch_config=batch_config,
        chunks_path=chunks_path,
        metadata_path=metadata_path,
        documents=tuple(documents),
        source_set_sha256=_source_set_hash(source_root),
    )
