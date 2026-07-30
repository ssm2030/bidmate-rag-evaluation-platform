from __future__ import annotations

import csv
import hashlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from bidmate_rag.eval_dataset.contract.legacy_export import LEGACY_COLUMNS
from bidmate_rag.eval_dataset.contract.models import Document, EvalItem
from bidmate_rag.eval_dataset.contract.package_io import read_package, write_package
from bidmate_rag.eval_dataset.review.repository import ReviewRepository
from bidmate_rag.evaluation.dataset import load_eval_samples
from bidmate_rag.evaluation.judge_v2 import LLMJudgeV2


def _item(document: Document, ordinal: int, *, answerability: str) -> EvalItem:
    anchors = []
    if answerability == "answerable":
        anchors = [
            {
                "anchor_id": str(uuid5(NAMESPACE_URL, f"p6-anchor-{ordinal}")),
                "ordinal": 0,
                "document_id": str(document.document_id),
                "pdf_page_number": 2,
                "printed_page_label": "2",
                "exact_quote": "Evidence quote",
                "context_before": "Before",
                "context_after": "After",
                "role": "support",
                "required": True,
                "resolution_status": "resolved",
                "resolution_method": "exact",
                "document_sha256": document.sha256,
                "resolver_version": "p6",
                "bbox": None,
            }
        ]
    return EvalItem.model_validate(
        {
            "item_id": str(uuid5(NAMESPACE_URL, f"p6-item-{ordinal}")),
            "revision": 1,
            "status": "needs_review",
            "question": f"Question {ordinal}?",
            "ground_truth_answer": f"Answer {ordinal}",
            "task_kind": "extract",
            "document_scope": "single",
            "answerability": answerability,
            "evidence_mode": "direct_quote" if anchors else "none",
            "perturbation": "none",
            "difficulty": "medium",
            "metadata_filter": {"year": 2026},
            "history": [{"role": "user", "content": "Prior turn"}],
            "verification_notes": ["verified"],
            "provenance": {"p6": "compatibility"},
            "evidence_anchors": anchors,
        }
    )


class _FakeCompletions:
    def __init__(self) -> None:
        self.request: dict | None = None

    def create(self, **kwargs):
        self.request = kwargs
        body = {
            "evidence": {
                "claims": [{"is_supported": True}],
                "required_items": [{"is_answered": True}],
                "gt_facts": [{"is_covered": True, "is_matched": True}],
                "contexts": [{"is_relevant": True}],
                "missing_facts": [],
            }
        }
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(body)))],
            usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0, prompt_tokens_details=None),
        )


def test_approved_snapshots_preserve_legacy_loader_and_judge_contract(tmp_path: Path) -> None:
    document = Document(
        document_id=uuid5(NAMESPACE_URL, "p6-document"),
        relative_pdf_path="public/rfp.pdf",
        sha256="a" * 64,
        page_count=2,
        legacy_filename="rfp.pdf",
        external_ids={},
        source_classification="public",
        external_transmission_allowed=False,
    )
    package = write_package(
        tmp_path / "package",
        dataset_id=uuid5(NAMESPACE_URL, "p6-dataset"),
        documents=[document],
        items=[
            _item(document, 1, answerability="answerable"),
            _item(document, 2, answerability="unanswerable"),
        ],
    )
    manifest_before = hashlib.sha256((package / "manifest.json").read_bytes()).hexdigest()
    repository = ReviewRepository(
        tmp_path / "review.sqlite3",
        export_root=tmp_path / "export",
    )
    dataset_id = repository.import_package(package)["dataset_id"]
    for item in repository.list_items(dataset_id):
        repository.approve(item["item_id"], base_revision=1)
    paths = repository.export_legacy(dataset_id)

    manifest_after = hashlib.sha256((package / "manifest.json").read_bytes()).hexdigest()
    assert manifest_after == manifest_before
    assert read_package(package)["manifest"]["schema_version"] == "2.0.0"
    with paths.standard.open(encoding="utf-8-sig", newline="") as handle:
        standard = list(csv.DictReader(handle))
    with paths.safety.open(encoding="utf-8-sig", newline="") as handle:
        safety = list(csv.DictReader(handle))
    assert list(standard[0]) == LEGACY_COLUMNS
    assert (
        paths.standard.read_text(encoding="utf-8-sig").splitlines()[0]
        == '"id","type","difficulty","question","ground_truth_answer","ground_truth_docs","metadata_filter","history","source_pages","reasoning_process","verification_points"'
    )
    assert len(standard) == 1 and len(safety) == 1
    assert standard[0]["source_pages"] == "[2]"
    assert json.loads(standard[0]["metadata_filter"]) == {"year": 2026}
    assert json.loads(standard[0]["history"]) == [{"role": "user", "content": "Prior turn"}]
    assert standard[0]["verification_points"] == "verified"
    assert safety[0]["type"] == "D"
    assert "unanswerable" not in standard[0]["question"].lower()

    loaded = load_eval_samples(paths.standard)
    assert loaded[0].question_id == standard[0]["id"]
    assert loaded[0].expected_doc_titles == ["rfp.pdf"]
    assert list(inspect.signature(LLMJudgeV2.evaluate).parameters) == [
        "self",
        "question",
        "answer",
        "contexts",
        "expected_answer",
    ]
    completions = _FakeCompletions()
    judge = LLMJudgeV2(client=SimpleNamespace(chat=SimpleNamespace(completions=completions)))
    scores = judge.evaluate("Question?", "Answer", ["Evidence"], "Answer")
    assert scores.answer_correctness == 1.0
    assert completions.request is not None


def test_program_two_has_no_streamlit_review_route() -> None:
    project_root = Path(__file__).resolve().parents[2]
    streamlit_source = (project_root / "app" / "eval_ui.py").read_text(encoding="utf-8")
    assert "eval_dataset.review" not in streamlit_source
    assert "eval-review" not in streamlit_source
