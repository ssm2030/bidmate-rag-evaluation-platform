from __future__ import annotations

from collections import Counter

from bidmate_rag.eval_dataset.automation.inventory import BatchInventory, InventoryDocument
from bidmate_rag.eval_dataset.automation.service import build_mock_candidates
from bidmate_rag.eval_dataset.contract.validation import validate_package_records


def _inventory_document(index: int) -> InventoryDocument:
    institution = f"기관{index}"
    project = f"디지털 서비스 고도화 {index}"
    quote = f"{project}의 계약기간은 착수일로부터 {90 + index}일이며 제안서 마감은 8월 {10 + index}일입니다."
    return InventoryDocument(
        source_filename=f"{institution}_{project}.json",
        relative_json_path=f"{institution}_{project}.json",
        relative_pdf_path=f"{institution}_{project}.pdf",
        institution_name=institution,
        project_name=project,
        source_fingerprint=f"{index + 10:064x}",
        document_sha256=f"{index + 1:064x}",
        page_count=1,
        page_texts=(quote,),
        source_page_texts=(quote,),
    )


def test_mock_candidates_use_real_inventory_and_exact_sop_distribution() -> None:
    inventory = BatchInventory(
        batch_id=1,
        representative_domain="기관1_디지털 서비스 고도화 1",
        documents=tuple(_inventory_document(index) for index in range(1, 5)),
    )

    generated = build_mock_candidates(inventory, target_count=30)

    assert len(generated.documents) == 4
    assert len(generated.items) == 30
    validate_package_records(generated.documents, generated.items)
    assert Counter(item.provenance["sop_type"] for item in generated.items) == {
        "A": 9,
        "B": 12,
        "C": 3,
        "D": 3,
        "E": 3,
    }
    assert Counter(item.difficulty for item in generated.items) == {
        "low": 15,
        "medium": 9,
        "high": 6,
    }
    assert all("Mock question" not in item.question for item in generated.items)
    assert all("mock/rfp.pdf" != document.relative_pdf_path for document in generated.documents)
    assert all(
        len(item.evidence_anchors) == 2
        for item in generated.items
        if item.provenance["sop_type"] == "B"
    )
    assert all(
        not item.evidence_anchors for item in generated.items if item.provenance["sop_type"] == "D"
    )
    assert all(item.history for item in generated.items if item.provenance["sop_type"] == "C")


def test_mock_candidates_are_byte_deterministic_at_model_boundary() -> None:
    inventory = BatchInventory(
        batch_id=2,
        representative_domain="기관1_디지털 서비스 고도화 1",
        documents=tuple(_inventory_document(index) for index in range(1, 4)),
    )

    first = build_mock_candidates(inventory, target_count=30)
    second = build_mock_candidates(inventory, target_count=30)

    assert [document.model_dump_json() for document in first.documents] == [
        document.model_dump_json() for document in second.documents
    ]
    assert [item.model_dump_json() for item in first.items] == [
        item.model_dump_json() for item in second.items
    ]
