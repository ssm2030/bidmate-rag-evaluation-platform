"""Transactional local review state without changing the frozen Schema v2 package."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from bidmate_rag.eval_dataset.contract.legacy_export import export_legacy
from bidmate_rag.eval_dataset.contract.models import BBox, Document, EvalItem, EvidenceAnchor
from bidmate_rag.eval_dataset.contract.package_io import read_package
from bidmate_rag.eval_dataset.pdf.resolver import resolve_anchor as auto_resolve_anchor
from bidmate_rag.eval_dataset.pdf.resolver import resolve_manually

from .db import ApprovalBlockedError, ReviewConflictError, ReviewDatabase
from .exporter import ApprovedExport
from .service import ReviewEvidenceService
from .sessions import ReviewSessions


class ReviewRepository:
    """Persist editable drafts locally and promote only immutable approved snapshots."""

    def __init__(
        self,
        database_path: Path | str,
        *,
        pdf_root: Path | str | None = None,
        package_root: Path | str | None = None,
        export_root: Path | str | None = None,
    ) -> None:
        self.database = ReviewDatabase(database_path)
        self.evidence_service = ReviewEvidenceService(pdf_root) if pdf_root is not None else None
        self.package_root = Path(package_root).resolve() if package_root is not None else None
        self.export_root = Path(export_root).resolve() if export_root is not None else None
        self.sessions = ReviewSessions(self.database)

    @staticmethod
    def _encode(value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _decode(value: str) -> dict[str, Any]:
        return json.loads(value)

    @staticmethod
    def _package_identifier(relative_name: str) -> str:
        return hashlib.sha256(relative_name.encode("utf-8")).hexdigest()[:24]

    def _candidate_directories(self) -> list[Path]:
        if self.package_root is None or not self.package_root.is_dir():
            return []
        return sorted(
            (
                path
                for path in self.package_root.iterdir()
                if path.is_dir() and not path.name.startswith(".")
            ),
            key=lambda path: path.name.casefold(),
        )

    def _inspect_candidate(self, package: Path) -> dict[str, Any]:
        relative_name = package.relative_to(self.package_root).as_posix()
        card: dict[str, Any] = {
            "package_id": self._package_identifier(relative_name),
            "name": package.name,
            "status": "invalid",
            "blocking_reason": None,
            "dataset_id": None,
            "artifact_version": None,
            "created_at": None,
            "batch_id": None,
            "mode": None,
            "document_count": 0,
            "item_count": 0,
            "anchor_count": 0,
            "schema_status": "not_checked",
            "checksum_status": "not_checked",
            "pdf_hash_status": "not_checked",
        }
        try:
            loaded = read_package(package)
            card["schema_status"] = "pass"
            card["checksum_status"] = "pass"
            if self.evidence_service is not None:
                for document_data in loaded["documents"]:
                    document = Document.model_validate(document_data)
                    self.evidence_service.verify_document(
                        document.relative_pdf_path,
                        document.sha256,
                    )
            card["pdf_hash_status"] = (
                "pass" if self.evidence_service is not None else "not_configured"
            )
            manifest = loaded["manifest"]
            items = loaded["items"]
            first = items[0] if items else {}
            card.update(
                {
                    "status": "valid",
                    "dataset_id": manifest["dataset_id"],
                    "artifact_version": manifest.get("artifact_version"),
                    "created_at": manifest.get("created_at"),
                    "batch_id": first.get("metadata_filter", {}).get("batch_id"),
                    "mode": first.get("provenance", {}).get("mode")
                    or manifest.get("generation_profile", {}).get("provider"),
                    "document_count": len(loaded["documents"]),
                    "item_count": len(items),
                    "anchor_count": sum(len(item["evidence_anchors"]) for item in items),
                }
            )
        except Exception as error:
            message = str(error)
            card["blocking_reason"] = message
            if "checksum" in message.lower():
                card["checksum_status"] = "fail"
            elif "document_changed" in message.lower():
                card["schema_status"] = "pass"
                card["checksum_status"] = "pass"
                card["pdf_hash_status"] = "fail"
            else:
                card["schema_status"] = "fail"
        return card

    def discover_packages(self) -> list[dict[str, Any]]:
        return [self._inspect_candidate(path) for path in self._candidate_directories()]

    def _discovered_path(self, package_id: str) -> Path:
        if len(package_id) != 24 or any(
            character not in "0123456789abcdef" for character in package_id
        ):
            raise ValueError("invalid package identifier")
        for path in self._candidate_directories():
            relative_name = path.relative_to(self.package_root).as_posix()
            if self._package_identifier(relative_name) == package_id:
                return path
        raise KeyError("unknown package identifier")

    def import_discovered(
        self,
        package_id: str,
        *,
        actor_session_id: str = "",
    ) -> dict[str, Any]:
        package = self._discovered_path(package_id)
        card = self._inspect_candidate(package)
        if card["status"] != "valid":
            raise ValueError(card["blocking_reason"] or "package validation failed")
        return self.import_package(package, actor_session_id=actor_session_id)

    def import_package(
        self,
        package_path: Path | str,
        *,
        actor_session_id: str = "",
    ) -> dict[str, Any]:
        package = Path(package_path).resolve()
        if self.package_root is not None and not package.is_relative_to(self.package_root):
            raise PermissionError("package path is outside the configured package root")
        loaded = read_package(package)
        if self.evidence_service is not None:
            for document_data in loaded["documents"]:
                document = Document.model_validate(document_data)
                self.evidence_service.verify_document(
                    document.relative_pdf_path,
                    document.sha256,
                )
        manifest = loaded["manifest"]
        dataset_id = manifest["dataset_id"]
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT dataset_id FROM review_datasets WHERE dataset_id=?", (dataset_id,)
            ).fetchone()
            if existing:
                return self.get_dataset(dataset_id)
            connection.execute(
                "INSERT INTO review_datasets(dataset_id, schema_version, package_path, manifest_json) "
                "VALUES (?, ?, ?, ?)",
                (dataset_id, manifest["schema_version"], str(package), self._encode(manifest)),
            )
            for document in loaded["documents"]:
                Document.model_validate(document)
                connection.execute(
                    "INSERT INTO review_documents(document_id, dataset_id, document_json) VALUES (?, ?, ?)",
                    (document["document_id"], dataset_id, self._encode(document)),
                )
            for item in loaded["items"]:
                EvalItem.model_validate(item)
                connection.execute(
                    "INSERT INTO review_items(item_id, dataset_id, revision, status, item_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        item["item_id"],
                        dataset_id,
                        item["revision"],
                        item["status"],
                        self._encode(item),
                    ),
                )
            connection.execute(
                "INSERT INTO review_events(dataset_id, event_type, actor_session_id, payload_json) "
                "VALUES (?, 'package_imported', ?, ?)",
                (dataset_id, actor_session_id or None, self._encode({"package": package.name})),
            )
        return self.get_dataset(dataset_id)

    def get_dataset(self, dataset_id: str) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT dataset_id, schema_version, package_path, manifest_json, imported_at "
            "FROM review_datasets WHERE dataset_id=?",
            (dataset_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown dataset {dataset_id}")
        data = dict(row)
        data["manifest"] = self._decode(data.pop("manifest_json"))
        return data

    def _dataset_summary(self, dataset_id: str) -> dict[str, Any]:
        dataset = self.get_dataset(dataset_id)
        rows = self.database.connection.execute(
            "SELECT status, COUNT(*) AS count FROM review_items WHERE dataset_id=? GROUP BY status",
            (dataset_id,),
        ).fetchall()
        by_status = {row["status"]: row["count"] for row in rows}
        counts = {
            "total": sum(by_status.values()),
            "approved": by_status.get("approved", 0),
            "needs_review": by_status.get("needs_review", 0),
            "needs_anchor_fix": by_status.get("needs_anchor_fix", 0),
            "draft": by_status.get("draft", 0),
            "rejected": by_status.get("rejected", 0),
        }
        terminal = counts["approved"] + counts["rejected"]
        last_event = self.database.connection.execute(
            "SELECT created_at FROM review_events WHERE dataset_id=? "
            "ORDER BY event_id DESC LIMIT 1",
            (dataset_id,),
        ).fetchone()
        latest_export = self.database.connection.execute(
            "SELECT export_id, kind, relative_path, checksum, item_count, created_at FROM review_exports "
            "WHERE dataset_id=? ORDER BY created_at DESC LIMIT 1",
            (dataset_id,),
        ).fetchone()
        return {
            "dataset_id": dataset_id,
            "schema_version": dataset["schema_version"],
            "artifact_version": dataset["manifest"].get("artifact_version"),
            "imported_at": dataset["imported_at"],
            "counts": counts,
            "progress_percent": round(terminal * 100 / counts["total"]) if counts["total"] else 0,
            "last_reviewed_at": last_event["created_at"] if last_event else None,
            "export_state": dict(latest_export) if latest_export else None,
        }

    def list_datasets(self) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT dataset_id FROM review_datasets ORDER BY imported_at, dataset_id"
        ).fetchall()
        return [self._dataset_summary(row["dataset_id"]) for row in rows]

    def verified_pdf_path(self, dataset_id: str, document_id: str) -> Path:
        if self.evidence_service is None:
            raise ValueError("local PDF evidence service is not configured")
        row = self.database.connection.execute(
            "SELECT document_json FROM review_documents WHERE dataset_id=? AND document_id=?",
            (dataset_id, document_id),
        ).fetchone()
        if row is None:
            raise KeyError("unknown review document")
        document = Document.model_validate(self._decode(row["document_json"]))
        return self.evidence_service.verify_document(document.relative_pdf_path, document.sha256)

    def list_items(self, dataset_id: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT item_json FROM review_items WHERE dataset_id=? ORDER BY item_id", (dataset_id,)
        ).fetchall()
        return [self._decode(row["item_json"]) for row in rows]

    def query_items(
        self,
        dataset_id: str,
        *,
        status: str | None = None,
        sop_type: str | None = None,
        difficulty: str | None = None,
        document_id: str | None = None,
        blocking_reason: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        self.get_dataset(dataset_id)
        summaries = []
        for item in self.list_items(dataset_id):
            reason = self._approval_reason(item)
            document_ids = list(
                dict.fromkeys(anchor["document_id"] for anchor in item["evidence_anchors"])
            )
            summary = {
                "item_id": item["item_id"],
                "revision": item["revision"],
                "status": item["status"],
                "question": item["question"],
                "sop_type": item.get("provenance", {}).get("sop_type"),
                "difficulty": item.get("difficulty"),
                "answerability": item.get("answerability"),
                "document_ids": document_ids,
                "anchor_count": len(item["evidence_anchors"]),
                "blocking_reason": reason,
            }
            if status and summary["status"] != status:
                continue
            if sop_type and summary["sop_type"] != sop_type:
                continue
            if difficulty and summary["difficulty"] != difficulty:
                continue
            if document_id and document_id not in document_ids:
                continue
            if blocking_reason and summary["blocking_reason"] != blocking_reason:
                continue
            summaries.append(summary)
        start = (page - 1) * page_size
        return {
            "items": summaries[start : start + page_size],
            "total": len(summaries),
            "page": page,
            "page_size": page_size,
        }

    def get_resume(self, dataset_id: str) -> dict[str, Any]:
        self.get_dataset(dataset_id)
        saved = self.database.connection.execute(
            "SELECT item_id, anchor_id FROM review_resume WHERE dataset_id=?",
            (dataset_id,),
        ).fetchone()
        if saved is not None:
            return {
                "dataset_id": dataset_id,
                "item_id": saved["item_id"],
                "anchor_id": saved["anchor_id"],
            }
        items = self.list_items(dataset_id)
        if not items:
            raise KeyError("dataset has no review items")
        item = next(
            (
                candidate
                for candidate in items
                if candidate["status"] in {"needs_anchor_fix", "needs_review", "draft"}
            ),
            items[0],
        )
        anchors = item["evidence_anchors"]
        anchor = next(
            (value for value in anchors if value.get("required")), anchors[0] if anchors else None
        )
        return {
            "dataset_id": dataset_id,
            "item_id": item["item_id"],
            "anchor_id": anchor["anchor_id"] if anchor else None,
        }

    def set_resume(self, dataset_id: str, item_id: str, anchor_id: str | None) -> dict[str, Any]:
        row = self._row(item_id)
        if row["dataset_id"] != dataset_id:
            raise ValueError("resume item does not belong to the dataset")
        item = self._decode(row["item_json"])
        if anchor_id is not None and anchor_id not in {
            anchor["anchor_id"] for anchor in item["evidence_anchors"]
        }:
            raise ValueError("resume anchor does not belong to the item")
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO review_resume(dataset_id, item_id, anchor_id, updated_at) "
                "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(dataset_id) DO UPDATE SET item_id=excluded.item_id, "
                "anchor_id=excluded.anchor_id, updated_at=CURRENT_TIMESTAMP",
                (dataset_id, item_id, anchor_id),
            )
        return {"dataset_id": dataset_id, "item_id": item_id, "anchor_id": anchor_id}

    def list_audit(self, item_id: str) -> list[dict[str, Any]]:
        row = self._row(item_id)
        events = self.database.connection.execute(
            "SELECT event_id, dataset_id, item_id, revision, event_type, actor_session_id, "
            "payload_json, created_at FROM review_events WHERE dataset_id=? AND "
            "(item_id=? OR item_id IS NULL) ORDER BY event_id DESC",
            (row["dataset_id"], item_id),
        ).fetchall()
        result = []
        for event in events:
            value = dict(event)
            value["payload"] = self._decode(value.pop("payload_json"))
            result.append(value)
        return result

    def _row(self, item_id: str):
        row = self.database.connection.execute(
            "SELECT item_id, dataset_id, revision, status, item_json, parent_snapshot_id "
            "FROM review_items WHERE item_id=?",
            (item_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown review item {item_id}")
        return row

    def get_item(self, item_id: str) -> dict[str, Any]:
        row = self._row(item_id)
        return self._decode(row["item_json"])

    def _save(
        self,
        row: Any,
        value: dict[str, Any],
        *,
        expected_revision: int,
        event_type: str,
        actor_session_id: str = "",
        event_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if row["revision"] != expected_revision:
            raise ReviewConflictError(
                f"stale revision {expected_revision}; latest revision is {row['revision']}"
            )
        value["revision"] = expected_revision + 1
        encoded = self._encode(value)
        with self.database.transaction() as connection:
            updated = connection.execute(
                "UPDATE review_items SET revision=?, status=?, item_json=? "
                "WHERE item_id=? AND revision=?",
                (value["revision"], value["status"], encoded, row["item_id"], expected_revision),
            ).rowcount
            if updated != 1:
                raise ReviewConflictError("review item changed during save")
            connection.execute(
                "INSERT INTO review_events(dataset_id, item_id, revision, event_type, "
                "actor_session_id, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    row["dataset_id"],
                    row["item_id"],
                    value["revision"],
                    event_type,
                    actor_session_id or None,
                    self._encode(event_payload or {}),
                ),
            )
        return value

    def save_draft(
        self,
        item_id: str,
        *,
        base_revision: int,
        patch: dict[str, Any],
        actor_session_id: str = "",
    ) -> dict[str, Any]:
        row = self._row(item_id)
        if row["status"] in {"approved", "rejected"}:
            raise PermissionError("approved or rejected snapshots are immutable; fork to edit")
        item = self._decode(row["item_json"])
        allowed = {
            "question",
            "ground_truth_answer",
            "task_kind",
            "difficulty",
            "answerability",
            "evidence_mode",
            "perturbation",
            "verification_notes",
            "evidence_anchors",
            "metadata_filter",
            "history",
        }
        unexpected = set(patch) - allowed
        if unexpected:
            raise ValueError(f"unsupported draft fields: {', '.join(sorted(unexpected))}")
        item.update(patch)
        item["status"] = "draft"
        return self._save(
            row,
            item,
            expected_revision=base_revision,
            event_type="draft_saved",
            actor_session_id=actor_session_id,
            event_payload={"fields": sorted(patch)},
        )

    def _document_for_anchor(self, dataset_id: str, anchor: dict[str, Any]) -> Document:
        row = self.database.connection.execute(
            "SELECT document_json FROM review_documents WHERE dataset_id=? AND document_id=?",
            (dataset_id, anchor["document_id"]),
        ).fetchone()
        if row is None:
            raise KeyError("anchor document is not available in this review dataset")
        return Document.model_validate(self._decode(row["document_json"]))

    def _current_document_hash(self, document: Document) -> str:
        if self.evidence_service is None:
            return document.sha256
        _, digest = self.evidence_service.document_sha256(document.relative_pdf_path)
        return digest

    def _persist_resolved_anchor(
        self,
        row: Any,
        item: dict[str, Any],
        anchor_id: str,
        resolved: EvidenceAnchor,
        *,
        base_revision: int,
        actor_session_id: str = "",
    ) -> dict[str, Any]:
        found = False
        anchors = []
        for anchor in item["evidence_anchors"]:
            if anchor["anchor_id"] == anchor_id:
                found = True
                anchors.append(resolved.model_dump(mode="json"))
            else:
                anchors.append(anchor)
        if not found:
            raise KeyError(f"unknown anchor {anchor_id}")
        item["evidence_anchors"] = anchors
        item["status"] = "needs_review"
        return self._save(
            row,
            item,
            expected_revision=base_revision,
            event_type="anchor_resolved",
            actor_session_id=actor_session_id,
            event_payload={"anchor_id": anchor_id, "method": resolved.resolution_method},
        )

    def resolve_anchor(
        self,
        item_id: str,
        anchor_id: str,
        *,
        base_revision: int,
        method: str,
        bbox: dict[str, Any],
        selected_quote: str,
        page_number: int,
        actor_session_id: str = "",
    ) -> dict[str, Any]:
        if method not in {"bbox", "manual"}:
            raise ValueError("manual reviewer resolution requires bbox or manual method")
        row = self._row(item_id)
        if row["status"] in {"approved", "rejected"}:
            raise PermissionError("approved or rejected snapshots are immutable; fork to edit")
        box = BBox.model_validate(bbox)
        item = self._decode(row["item_json"])
        anchor_data = next(
            (anchor for anchor in item["evidence_anchors"] if anchor["anchor_id"] == anchor_id),
            None,
        )
        if anchor_data is None:
            raise KeyError(f"unknown anchor {anchor_id}")
        if selected_quote.strip() != anchor_data["exact_quote"].strip():
            raise ValueError("manual selection must exactly match the anchor quote")
        if page_number != anchor_data["pdf_page_number"]:
            raise ValueError("manual selection page must match the anchor page")
        document = self._document_for_anchor(row["dataset_id"], anchor_data)
        if page_number > document.page_count:
            raise ValueError("manual selection page is outside the local PDF bounds")
        current_hash = self._current_document_hash(document)
        resolved = resolve_manually(EvidenceAnchor.model_validate(anchor_data), box, current_hash)
        if method == "bbox" and resolved.resolution_status == "resolved":
            resolved = resolved.model_copy(update={"resolution_method": "bbox"})
        return self._persist_resolved_anchor(
            row,
            item,
            anchor_id,
            resolved,
            base_revision=base_revision,
            actor_session_id=actor_session_id,
        )

    def auto_resolve_anchor(
        self,
        item_id: str,
        anchor_id: str,
        *,
        base_revision: int,
        actor_session_id: str = "",
    ) -> dict[str, Any]:
        if self.evidence_service is None:
            raise ValueError("local PDF evidence service is not configured")
        row = self._row(item_id)
        if row["status"] in {"approved", "rejected"}:
            raise PermissionError("approved or rejected snapshots are immutable; fork to edit")
        item = self._decode(row["item_json"])
        anchor_data = next(
            (anchor for anchor in item["evidence_anchors"] if anchor["anchor_id"] == anchor_id),
            None,
        )
        if anchor_data is None:
            raise KeyError(f"unknown anchor {anchor_id}")
        document = self._document_for_anchor(row["dataset_id"], anchor_data)
        current_hash, pages = self.evidence_service.pages(
            document.relative_pdf_path, document.sha256
        )
        anchor = EvidenceAnchor.model_validate(anchor_data)
        if not 1 <= anchor.pdf_page_number <= len(pages):
            raise ValueError("anchor page is outside the local PDF bounds")
        resolved = auto_resolve_anchor(anchor, pages[anchor.pdf_page_number - 1].text, current_hash)
        return self._persist_resolved_anchor(
            row,
            item,
            anchor_id,
            resolved,
            base_revision=base_revision,
            actor_session_id=actor_session_id,
        )

    @staticmethod
    def _approval_reason(item: dict[str, Any]) -> str | None:
        statuses = {anchor["resolution_status"] for anchor in item["evidence_anchors"]}
        for blocked in ("document_changed", "unresolved", "ambiguous"):
            if blocked in statuses:
                return blocked
        try:
            EvalItem.model_validate({**item, "status": "approved"})
        except Exception as error:  # Pydantic presents the actionable contract violation.
            return str(error)
        return None

    def _snapshot(
        self, item_id: str, revision: int, action: str, value: dict[str, Any], note: str = ""
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO review_snapshots(item_id, revision, action, item_json, note) VALUES (?, ?, ?, ?, ?)",
                (item_id, revision, action, self._encode(value), note),
            )

    def approve(
        self,
        item_id: str,
        *,
        base_revision: int,
        actor_session_id: str = "",
    ) -> dict[str, Any]:
        row = self._row(item_id)
        reason = self._approval_reason(self._decode(row["item_json"]))
        if reason:
            raise ApprovalBlockedError(f"approval blocked: {reason}")
        item = self._decode(row["item_json"])
        item["status"] = "approved"
        approved = self._save(
            row,
            item,
            expected_revision=base_revision,
            event_type="approved",
            actor_session_id=actor_session_id,
        )
        self._snapshot(item_id, approved["revision"], "approved", approved)
        return approved

    def reject(
        self,
        item_id: str,
        *,
        base_revision: int,
        reason: str = "",
        actor_session_id: str = "",
    ) -> dict[str, Any]:
        if not reason.strip():
            raise ValueError("rejection reason is required")
        row = self._row(item_id)
        item = self._decode(row["item_json"])
        item["status"] = "rejected"
        rejected = self._save(
            row,
            item,
            expected_revision=base_revision,
            event_type="rejected",
            actor_session_id=actor_session_id,
            event_payload={"reason": reason.strip()},
        )
        self._snapshot(item_id, rejected["revision"], "rejected", rejected, reason)
        return rejected

    def fork(
        self,
        item_id: str,
        *,
        base_revision: int,
        actor_session_id: str = "",
    ) -> dict[str, Any]:
        row = self._row(item_id)
        if row["revision"] != base_revision:
            raise ReviewConflictError(
                f"stale revision {base_revision}; latest revision is {row['revision']}"
            )
        if row["status"] != "approved":
            raise ValueError("only approved snapshots may be forked")
        source = self._decode(row["item_json"])
        snapshot = self.database.connection.execute(
            "SELECT snapshot_id FROM review_snapshots WHERE item_id=? AND action='approved' "
            "ORDER BY snapshot_id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        if snapshot is None:
            raise RuntimeError("approved item has no immutable snapshot")
        fork = {
            **source,
            "item_id": str(uuid4()),
            "revision": 1,
            "status": "draft",
            "provenance": {
                **source.get("provenance", {}),
                "forked_from_snapshot": snapshot["snapshot_id"],
            },
        }
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO review_items(item_id, dataset_id, revision, status, item_json, parent_snapshot_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    fork["item_id"],
                    row["dataset_id"],
                    1,
                    "draft",
                    self._encode(fork),
                    str(snapshot["snapshot_id"]),
                ),
            )
            connection.execute(
                "INSERT INTO review_events(dataset_id, item_id, revision, event_type, "
                "actor_session_id, payload_json) VALUES (?, ?, 1, 'forked', ?, ?)",
                (
                    row["dataset_id"],
                    fork["item_id"],
                    actor_session_id or None,
                    self._encode({"parent_snapshot_id": snapshot["snapshot_id"]}),
                ),
            )
        self._snapshot(
            fork["item_id"], 1, "forked", fork, f"from snapshot {snapshot['snapshot_id']}"
        )
        return fork

    def list_snapshots(self, item_id: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT snapshot_id, item_id, revision, action, item_json, note, created_at "
            "FROM review_snapshots WHERE item_id=? ORDER BY snapshot_id DESC",
            (item_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def export_legacy(
        self,
        dataset_id: str,
        *,
        actor_session_id: str = "",
    ) -> ApprovedExport:
        if self.export_root is None:
            raise ValueError("server export root is not configured")
        items = self.list_items(dataset_id)
        pending = [
            item["item_id"] for item in items if item["status"] not in {"approved", "rejected"}
        ]
        if pending:
            raise ApprovalBlockedError("export blocked: unresolved review items remain")

        approved_ids = sorted(item["item_id"] for item in items if item["status"] == "approved")
        snapshot_rows: list[tuple[str, str]] = []
        snapshots: list[EvalItem] = []
        for item_id in approved_ids:
            row = self.database.connection.execute(
                "SELECT item_json FROM review_snapshots WHERE item_id=? AND action='approved' "
                "ORDER BY snapshot_id DESC LIMIT 1",
                (item_id,),
            ).fetchone()
            if row is None:
                raise ApprovalBlockedError("export blocked: missing approved snapshot")
            snapshot_rows.append((item_id, row["item_json"]))
            snapshots.append(EvalItem.model_validate(self._decode(row["item_json"])))

        snapshot_material = self._encode(
            {
                "dataset_id": dataset_id,
                "approved_snapshots": [
                    {"item_id": item_id, "item_json": item_json}
                    for item_id, item_json in snapshot_rows
                ],
            }
        ).encode("utf-8")
        export_id = hashlib.sha256(snapshot_material).hexdigest()
        export_directory = self.export_root / dataset_id / export_id[:16]

        document_rows = self.database.connection.execute(
            "SELECT document_json FROM review_documents WHERE dataset_id=?", (dataset_id,)
        ).fetchall()
        documents = {
            document.document_id: document
            for document in (
                Document.model_validate(self._decode(row["document_json"])) for row in document_rows
            )
        }
        paths = export_legacy(export_directory, snapshots, documents)
        standard_checksum = hashlib.sha256(paths.standard.read_bytes()).hexdigest()
        safety_checksum = hashlib.sha256(paths.safety.read_bytes()).hexdigest()
        checksum = hashlib.sha256(
            f"{standard_checksum}:{safety_checksum}".encode("ascii")
        ).hexdigest()
        relative_path = export_directory.relative_to(self.export_root).as_posix()

        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT checksum, item_count, relative_path FROM review_exports WHERE export_id=?",
                (export_id,),
            ).fetchone()
            expected = (checksum, len(snapshots), relative_path)
            if existing is not None:
                actual = (existing["checksum"], existing["item_count"], existing["relative_path"])
                if actual != expected:
                    raise RuntimeError(
                        "recorded export does not match deterministic snapshot output"
                    )
            else:
                connection.execute(
                    "INSERT INTO review_exports(export_id, dataset_id, kind, relative_path, "
                    "checksum, item_count, actor_session_id) VALUES (?, ?, 'legacy_v1', ?, ?, ?, ?)",
                    (
                        export_id,
                        dataset_id,
                        relative_path,
                        checksum,
                        len(snapshots),
                        actor_session_id or None,
                    ),
                )
                connection.execute(
                    "INSERT INTO review_events(dataset_id, event_type, actor_session_id, payload_json) "
                    "VALUES (?, 'exported', ?, ?)",
                    (
                        dataset_id,
                        actor_session_id or None,
                        self._encode({"export_id": export_id, "checksum": checksum}),
                    ),
                )
        return ApprovedExport(
            export_id=export_id,
            standard=paths.standard,
            safety=paths.safety,
            checksum=checksum,
            item_count=len(snapshots),
            relative_path=relative_path,
        )

    def get_export(self, export_id: str) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT export_id, dataset_id, kind, relative_path, checksum, item_count, created_at "
            "FROM review_exports WHERE export_id=?",
            (export_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown export {export_id}")
        return dict(row)
