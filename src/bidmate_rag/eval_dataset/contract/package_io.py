"""Atomic local Schema v2 package read/write with checksum and contract verification."""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any, Sequence
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker

from .canonical_json import canonical_json, canonical_jsonl, sha256_bytes
from .hashing import source_set_hash
from .models import Document, EvalItem
from .validation import validate_package_records

DATA_FILES = ("documents.jsonl", "items.jsonl", "generation_events.jsonl")
ROOT_FILES = {"manifest.json", "checksums.json", *DATA_FILES}
SCHEMA_ROOT = Path(__file__).resolve().parents[4] / "schemas" / "eval_dataset" / "v2"


def _records(path: Path) -> list[dict[str, Any]]:
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSONL in {path.name}") from exc


def _validate_schema(schema_name: str, value: Any) -> None:
    schema = json.loads((SCHEMA_ROOT / f"{schema_name}.schema.json").read_text(encoding="utf-8"))
    try:
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)
    except Exception as exc:
        raise ValueError(
            f"{schema_name} schema validation failed: {exc.message if hasattr(exc, 'message') else exc}"
        ) from exc


def write_package(
    destination: Path | str,
    *,
    dataset_id: UUID,
    documents: Sequence[Document],
    items: Sequence[EvalItem],
) -> Path:
    """Write a candidate package only after cross-record validation and canonical checksums."""
    validate_package_records(documents, items)
    destination = Path(destination)
    if destination.exists():
        raise ValueError("destination package already exists")
    temporary = destination.parent / f".tmp-{uuid.uuid4()}"
    temporary.mkdir(parents=True)
    try:
        payloads = {
            "documents.jsonl": canonical_jsonl(
                document.model_dump(mode="json") for document in documents
            ),
            "items.jsonl": canonical_jsonl(item.model_dump(mode="json") for item in items),
            "generation_events.jsonl": b"",
        }
        digests = {name: sha256_bytes(content) for name, content in payloads.items()}
        for name, content in payloads.items():
            (temporary / name).write_bytes(content)
        checksums = {"files": digests}
        checksum_bytes = canonical_json(checksums)
        (temporary / "checksums.json").write_bytes(checksum_bytes)
        files = [
            {
                "path": name,
                "media_type": "application/jsonl",
                "record_count": len(content.splitlines()),
                "sha256": digests[name],
            }
            for name, content in payloads.items()
        ] + [
            {
                "path": "checksums.json",
                "media_type": "application/json",
                "record_count": None,
                "sha256": sha256_bytes(checksum_bytes),
            }
        ]
        manifest = {
            "dataset_id": str(dataset_id),
            "schema_version": "2.0.0",
            "taxonomy_version": "1.0.0",
            "artifact_version": 1,
            "parent_artifact_version": None,
            "source_set_hash": source_set_hash(documents),
            "generation_profile": {
                "profile_id": "local-mock-v1",
                "prompt_versions": {},
                "provider": "mock",
                "model": "mock",
                "parameters": {},
            },
            "created_at": "1970-01-01T00:00:00Z",
            "status": "candidate",
            "files": files,
        }
        (temporary / "manifest.json").write_bytes(canonical_json(manifest))
        temporary.replace(destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination


def read_package(package: Path | str) -> dict[str, Any]:
    """Read only a complete Schema v2 package that passes checksums, schemas and cross references."""
    package = Path(package)
    if not package.is_dir():
        raise ValueError("package directory is missing")
    actual_files = {path.name for path in package.iterdir() if path.is_file()}
    if actual_files != ROOT_FILES:
        raise ValueError("package file set is incomplete or contains unexpected files")
    try:
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        checksums_payload = json.loads((package / "checksums.json").read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("invalid package JSON") from exc
    _validate_schema("manifest", manifest)
    _validate_schema("checksums", checksums_payload)
    if manifest["schema_version"] != "2.0.0":
        raise ValueError("unsupported schema version")
    checksums = checksums_payload["files"]
    listed = {entry["path"]: entry for entry in manifest["files"]}
    if set(listed) != {*DATA_FILES, "checksums.json"} or len(listed) != len(manifest["files"]):
        raise ValueError("manifest file inventory is incomplete")
    records = {name: _records(package / name) for name in DATA_FILES}
    for name in DATA_FILES:
        content = (package / name).read_bytes()
        actual = sha256_bytes(content)
        if checksums.get(name) != actual or listed[name].get("sha256") != actual:
            raise ValueError(f"checksum mismatch for {name}")
        if listed[name].get("record_count") != len(records[name]):
            raise ValueError(f"record count mismatch for {name}")
    checksum_actual = sha256_bytes((package / "checksums.json").read_bytes())
    if listed["checksums.json"].get("sha256") != checksum_actual:
        raise ValueError("checksum mismatch for checksums.json")
    for document in records["documents.jsonl"]:
        _validate_schema("document", document)
    for item in records["items.jsonl"]:
        _validate_schema("item", item)
    for event in records["generation_events.jsonl"]:
        _validate_schema("generation_event", event)
    documents = [Document.model_validate(document) for document in records["documents.jsonl"]]
    items = [EvalItem.model_validate(item) for item in records["items.jsonl"]]
    validate_package_records(documents, items)
    item_ids = {str(item.item_id) for item in items}
    if any(event["item_id"] not in item_ids for event in records["generation_events.jsonl"]):
        raise ValueError("generation event references an unknown item")
    return {
        "manifest": manifest,
        "documents": records["documents.jsonl"],
        "items": records["items.jsonl"],
        "generation_events": records["generation_events.jsonl"],
    }
