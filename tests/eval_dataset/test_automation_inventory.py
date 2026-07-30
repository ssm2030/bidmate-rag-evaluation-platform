from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from bidmate_rag.eval_dataset.automation import inventory


def _write_pdf(path: Path, text: str) -> bytes:
    stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    ]
    data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n".encode())
        data.extend(obj)
        data.extend(b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    data.extend(b"0000000000 65535 f \n")
    for offset in offsets:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return bytes(data)


def test_inventory_uses_only_selected_batch_and_locks_matching_pdf(tmp_path) -> None:
    assert hasattr(inventory, "inventory_batch"), "Batch inventory is missing"
    json_root = tmp_path / "json"
    pdf_root = tmp_path / "pdf"
    json_root.mkdir()
    pdf_root.mkdir()
    selected = "기관A_사업A.json"
    excluded = "기관B_사업B.json"
    (json_root / selected).write_text(
        json.dumps({"content": "Delivery date is 2026-08-31."}), encoding="utf-8"
    )
    (json_root / excluded).write_text(json.dumps({"content": "Excluded"}), encoding="utf-8")
    pdf_bytes = _write_pdf(pdf_root / "기관A_사업A.pdf", "Delivery date is 2026-08-31.")
    config = tmp_path / "Batch_config.json"
    config.write_text(
        json.dumps(
            [
                {
                    "batch_id": 1,
                    "count": 1,
                    "representative_domain": "기관A_사업A",
                    "files": [selected],
                },
                {
                    "batch_id": 2,
                    "count": 1,
                    "representative_domain": "기관B_사업B",
                    "files": [excluded],
                },
            ]
        ),
        encoding="utf-8",
    )

    result = inventory.inventory_batch(config, json_root=json_root, pdf_root=pdf_root, batch_id=1)
    assert result.batch_id == 1
    assert len(result.documents) == 1
    document = result.documents[0]
    assert document.source_filename == selected
    assert document.institution_name == "기관A"
    assert document.project_name == "사업A"
    assert document.relative_pdf_path == "기관A_사업A.pdf"
    assert document.document_sha256 == sha256(pdf_bytes).hexdigest()
    assert document.page_count == 1


def test_inventory_rejects_missing_or_duplicate_pdf(tmp_path) -> None:
    assert hasattr(inventory, "inventory_batch"), "Batch inventory is missing"
    json_root = tmp_path / "json"
    pdf_root = tmp_path / "pdf"
    json_root.mkdir()
    pdf_root.mkdir()
    filename = "기관_사업.json"
    (json_root / filename).write_text("{}", encoding="utf-8")
    config = tmp_path / "Batch_config.json"
    config.write_text(
        json.dumps(
            [{"batch_id": 1, "count": 1, "representative_domain": "기관_사업", "files": [filename]}]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="PDF"):
        inventory.inventory_batch(config, json_root=json_root, pdf_root=pdf_root, batch_id=1)


def test_inventory_normalizes_unicode_filename_variants_for_json_and_pdf(tmp_path) -> None:
    json_root = tmp_path / "json"
    pdf_root = tmp_path / "pdf"
    json_root.mkdir()
    pdf_root.mkdir()
    configured = "(사）한국대학스포츠협의회_KUSF 기록관리.json"
    actual = "(사)한국대학스포츠협의회_KUSF 기록관리.json"
    quote = "Contract period is 120 days and proposal closes August 30."
    (json_root / actual).write_text(
        json.dumps({"pages": [{"page_num": 1, "text": quote}]}),
        encoding="utf-8",
    )
    _write_pdf(pdf_root / "(사)한국대학스포츠협의회_KUSF 기록관리.pdf", quote)
    config = tmp_path / "Batch_config.json"
    config.write_text(
        json.dumps(
            [
                {
                    "batch_id": 1,
                    "count": 1,
                    "representative_domain": "KUSF",
                    "files": [configured],
                }
            ]
        ),
        encoding="utf-8",
    )

    result = inventory.inventory_batch(
        config,
        json_root=json_root,
        pdf_root=pdf_root,
        batch_id=1,
    )

    assert result.documents[0].relative_json_path == actual


def test_inventory_maps_unique_institution_alias_by_long_project_prefix(tmp_path) -> None:
    json_root = tmp_path / "json"
    pdf_root = tmp_path / "pdf"
    json_root.mkdir()
    pdf_root.mkdir()
    configured = "BioAlias_Medical information system improvement phase two.json"
    actual = "HealthAgency_Medical information system improvement.json"
    quote = "Contract period is 150 days and the budget is 352 million won."
    (json_root / actual).write_text(
        json.dumps({"pages": [{"page_num": 1, "text": quote}]}),
        encoding="utf-8",
    )
    _write_pdf(pdf_root / "HealthAgency_Medical information system improvement.pdf", quote)
    config = tmp_path / "Batch_config.json"
    config.write_text(
        json.dumps(
            [
                {
                    "batch_id": 1,
                    "count": 1,
                    "representative_domain": "BioAlias",
                    "files": [configured],
                }
            ]
        ),
        encoding="utf-8",
    )

    result = inventory.inventory_batch(
        config,
        json_root=json_root,
        pdf_root=pdf_root,
        batch_id=1,
    )

    document = result.documents[0]
    assert document.source_filename == actual
    assert document.institution_name == "HealthAgency"
    assert document.relative_pdf_path == "HealthAgency_Medical information system improvement.pdf"


def test_inventory_rejects_ambiguous_project_alias_mapping(tmp_path) -> None:
    json_root = tmp_path / "json"
    pdf_root = tmp_path / "pdf"
    json_root.mkdir()
    pdf_root.mkdir()
    configured = "Alias_Medical information system improvement phase two.json"
    quote = "Contract period is 150 days and the budget is 352 million won."
    for agency in ("AgencyA", "AgencyB"):
        actual = f"{agency}_Medical information system improvement.json"
        (json_root / actual).write_text(
            json.dumps({"pages": [{"page_num": 1, "text": quote}]}),
            encoding="utf-8",
        )
        _write_pdf(pdf_root / f"{agency}_Medical information system improvement.pdf", quote)
    config = tmp_path / "Batch_config.json"
    config.write_text(
        json.dumps(
            [
                {
                    "batch_id": 1,
                    "count": 1,
                    "representative_domain": "Alias",
                    "files": [configured],
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="JSON mapping requires exactly one"):
        inventory.inventory_batch(
            config,
            json_root=json_root,
            pdf_root=pdf_root,
            batch_id=1,
        )


def test_inventory_reuses_pdf_text_by_document_hash(tmp_path, monkeypatch) -> None:
    json_root = tmp_path / "json"
    pdf_root = tmp_path / "pdf"
    cache_root = tmp_path / "cache"
    json_root.mkdir()
    pdf_root.mkdir()
    filename = "Agency_Project.json"
    quote = "Contract period is 120 days and proposal closes August 30."
    (json_root / filename).write_text(
        json.dumps({"pages": [{"page_num": 1, "text": quote}]}),
        encoding="utf-8",
    )
    _write_pdf(pdf_root / "Agency_Project.pdf", quote)
    config = tmp_path / "Batch_config.json"
    config.write_text(
        json.dumps(
            [
                {
                    "batch_id": 1,
                    "count": 1,
                    "representative_domain": "Agency_Project",
                    "files": [filename],
                }
            ]
        ),
        encoding="utf-8",
    )
    calls = 0
    original = inventory.extract_pdf_pages

    def counted(path):
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(inventory, "extract_pdf_pages", counted)

    first = inventory.inventory_batch(
        config,
        json_root=json_root,
        pdf_root=pdf_root,
        batch_id=1,
        extraction_cache_root=cache_root,
    )
    second = inventory.inventory_batch(
        config,
        json_root=json_root,
        pdf_root=pdf_root,
        batch_id=1,
        extraction_cache_root=cache_root,
    )

    assert first == second
    assert calls == 1
    assert len(list(cache_root.rglob("*.json"))) == 1
