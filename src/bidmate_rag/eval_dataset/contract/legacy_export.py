"""Deterministic Schema v2 snapshot to legacy 11-column CSV exporter."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence
from uuid import UUID

from .models import Document, EvalItem

LEGACY_COLUMNS = [
    "id",
    "type",
    "difficulty",
    "question",
    "ground_truth_answer",
    "ground_truth_docs",
    "metadata_filter",
    "history",
    "source_pages",
    "reasoning_process",
    "verification_points",
]


@dataclass(frozen=True)
class LegacyExportPaths:
    standard: Path
    safety: Path


def _legacy_type(item: EvalItem) -> str:
    if item.perturbation != "none":
        return "E"
    if item.answerability in {"unanswerable", "contradiction"}:
        return "D"
    if item.task_kind == "follow_up":
        return "C"
    if item.task_kind == "compare":
        return "B"
    return "A"


def _row(index: int, item: EvalItem, documents: Mapping[UUID, Document]) -> dict[str, str]:
    anchors = sorted(item.evidence_anchors, key=lambda anchor: anchor.ordinal)
    doc_names = list(
        dict.fromkeys(documents[anchor.document_id].legacy_filename for anchor in anchors)
    )
    pages = [anchor.pdf_page_number for anchor in anchors]
    difficulty = {"low": "하", "medium": "중", "high": "상"}[item.difficulty]
    return {
        "id": f"Q{index:03d}",
        "type": _legacy_type(item),
        "difficulty": difficulty,
        "question": item.question,
        "ground_truth_answer": item.ground_truth_answer,
        "ground_truth_docs": json.dumps(doc_names, ensure_ascii=False, separators=(",", ":")),
        "metadata_filter": json.dumps(
            item.metadata_filter, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        "history": json.dumps(item.history, ensure_ascii=False, separators=(",", ":")),
        "source_pages": json.dumps(pages, ensure_ascii=False, separators=(",", ":")),
        "reasoning_process": "",
        "verification_points": "; ".join(item.verification_notes),
    }


def _write(path: Path, rows: Sequence[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=LEGACY_COLUMNS, quoting=csv.QUOTE_ALL, lineterminator="\r\n"
        )
        writer.writeheader()
        writer.writerows(rows)


def export_legacy(
    directory: Path | str, items: Sequence[EvalItem], documents: Mapping[UUID, Document]
) -> LegacyExportPaths:
    """Export approved snapshots without mixing abstention-only rows into standard scoring."""
    directory = Path(directory)
    standard_dir = directory / "legacy" / "standard"
    safety_dir = directory / "legacy" / "abstention_safety"
    standard_dir.mkdir(parents=True, exist_ok=True)
    safety_dir.mkdir(parents=True, exist_ok=True)
    standard = standard_dir / "eval_batch_01.csv"
    safety = safety_dir / "eval_batch_01.csv"
    approved = sorted(
        (item for item in items if item.status == "approved"), key=lambda item: str(item.item_id)
    )
    standard_rows = [
        _row(index + 1, item, documents)
        for index, item in enumerate(approved)
        if item.answerability != "unanswerable"
    ]
    safety_rows = [
        _row(index + 1, item, documents)
        for index, item in enumerate(approved)
        if item.answerability == "unanswerable"
    ]
    _write(standard, standard_rows)
    _write(safety, safety_rows)
    return LegacyExportPaths(standard=standard, safety=safety)
