from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4


@dataclass(frozen=True)
class WorkUnit:
    work_unit_id: str
    status: str
    attempts: int = 0
    last_error: str | None = None
    failure_stage: str | None = None


class AutomationLedger:
    """SQLite-backed, resumable local automation state with idempotent run and work identities."""

    TERMINAL_STATUSES = frozenset({"done", "needs_review", "permanent_failed"})

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            self.path,
            timeout=5,
            isolation_level=None,
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self._bootstrap()

    @contextmanager
    def _write(self) -> Iterator[None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def _bootstrap(self) -> None:
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS runs ("
            "run_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, cost_limit_microusd INTEGER NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'running', cost_microusd INTEGER NOT NULL DEFAULT 0, "
            "package_checksum TEXT, created_at TEXT NOT NULL DEFAULT '')"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS work_units ("
            "work_unit_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, idempotency_key TEXT UNIQUE NOT NULL, "
            "status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, "
            "FOREIGN KEY(run_id) REFERENCES runs(run_id))"
        )
        self._ensure_columns(
            "runs",
            {
                "status": "TEXT NOT NULL DEFAULT 'running'",
                "cost_microusd": "INTEGER NOT NULL DEFAULT 0",
                "package_checksum": "TEXT",
                "created_at": "TEXT NOT NULL DEFAULT ''",
                "identity_hash": "TEXT",
                "identity_json": "TEXT NOT NULL DEFAULT '{}'",
                "mode": "TEXT NOT NULL DEFAULT 'mock'",
            },
        )
        self._ensure_columns(
            "work_units",
            {
                "attempts": "INTEGER NOT NULL DEFAULT 0",
                "last_error": "TEXT",
                "failure_stage": "TEXT",
                "terminal_code": "TEXT",
                "result_json": "TEXT",
            },
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_work_units_run_id ON work_units(run_id)"
        )
        self.connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_identity_hash "
            "ON runs(identity_hash) WHERE identity_hash IS NOT NULL"
        )

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        known = {row[1] for row in self.connection.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in known:
                self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    @staticmethod
    def _work_unit(row: sqlite3.Row) -> WorkUnit:
        return WorkUnit(
            row["work_unit_id"],
            row["status"],
            row["attempts"],
            row["last_error"],
            row["failure_stage"],
        )

    def create_run(
        self,
        dataset_id: str,
        *,
        cost_limit_microusd: int,
        run_id: str | None = None,
        identity_hash: str | None = None,
        identity_json: str = "{}",
        mode: str = "mock",
    ) -> str:
        if cost_limit_microusd < 0:
            raise ValueError("cost limit must be non-negative")
        if mode not in {"mock", "live"}:
            raise ValueError("mode must be mock or live")
        run_id = run_id or str(uuid4())
        with self._write():
            self.connection.execute(
                "INSERT INTO runs ("
                "run_id, dataset_id, cost_limit_microusd, status, cost_microusd, created_at, "
                "identity_hash, identity_json, mode"
                ") VALUES (?, ?, ?, 'running', 0, ?, ?, ?, ?)",
                (
                    run_id,
                    dataset_id,
                    cost_limit_microusd,
                    datetime.now(UTC).isoformat(),
                    identity_hash,
                    identity_json,
                    mode,
                ),
            )
        return run_id

    @staticmethod
    def identity_hash(identity: dict[str, Any]) -> tuple[str, str]:
        identity_json = json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return identity_json, hashlib.sha256(identity_json.encode("utf-8")).hexdigest()

    def run_id_for_identity(self, identity: dict[str, Any]) -> str | None:
        _, identity_hash = self.identity_hash(identity)
        row = self.connection.execute(
            "SELECT run_id FROM runs WHERE identity_hash=? LIMIT 1",
            (identity_hash,),
        ).fetchone()
        return None if row is None else str(row["run_id"])

    def get_or_create_run_for_identity(
        self,
        *,
        dataset_id: str,
        identity: dict[str, Any],
        cost_limit_microusd: int,
    ) -> str:
        identity_json, identity_hash = self.identity_hash(identity)
        row = self.connection.execute(
            "SELECT run_id FROM runs WHERE identity_hash=? LIMIT 1",
            (identity_hash,),
        ).fetchone()
        if row:
            return str(row["run_id"])
        return self.create_run(
            dataset_id,
            cost_limit_microusd=cost_limit_microusd,
            identity_hash=identity_hash,
            identity_json=identity_json,
            mode=str(identity.get("mode", "mock")),
        )

    def get_or_create_run(self, dataset_id: str, *, cost_limit_microusd: int) -> str:
        row = self.connection.execute(
            "SELECT run_id FROM runs WHERE dataset_id=? ORDER BY created_at DESC LIMIT 1",
            (dataset_id,),
        ).fetchone()
        return (
            str(row["run_id"])
            if row
            else self.create_run(dataset_id, cost_limit_microusd=cost_limit_microusd)
        )

    def run_id_for_dataset(self, dataset_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT run_id FROM runs WHERE dataset_id=? ORDER BY created_at DESC LIMIT 1",
            (dataset_id,),
        ).fetchone()
        return None if row is None else str(row["run_id"])

    def create_work_unit(
        self,
        run_id: str,
        *,
        ordinal: int,
        plan: dict[str, Any],
        prompt_bundle_hash: str,
    ) -> WorkUnit:
        plan_json = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        key = hashlib.sha256(
            f"{run_id}\0{ordinal}\0{plan_json}\0{prompt_bundle_hash}".encode()
        ).hexdigest()
        with self._write():
            row = self.connection.execute(
                "SELECT work_unit_id, status, attempts, last_error, failure_stage "
                "FROM work_units WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if row:
                return self._work_unit(row)
            work_unit_id = str(uuid4())
            self.connection.execute(
                "INSERT INTO work_units "
                "(work_unit_id, run_id, idempotency_key, status, attempts) "
                "VALUES (?, ?, ?, 'planned', 0)",
                (work_unit_id, run_id, key),
            )
            return WorkUnit(work_unit_id, "planned")

    def claim(self, work_unit_id: str) -> WorkUnit:
        with self._write():
            row = self.connection.execute(
                "SELECT w.work_unit_id, w.status, w.attempts, w.last_error, "
                "w.failure_stage, r.status AS run_status "
                "FROM work_units w JOIN runs r ON r.run_id=w.run_id "
                "WHERE w.work_unit_id=?",
                (work_unit_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown work unit")
            if row["run_status"] == "cost_paused" and row["status"] not in self.TERMINAL_STATUSES:
                return self._work_unit(row)
            if row["status"] in {"planned", "retryable_failed"}:
                self.connection.execute(
                    "UPDATE work_units SET status='running', attempts=attempts+1 "
                    "WHERE work_unit_id=?",
                    (work_unit_id,),
                )
            updated = self.connection.execute(
                "SELECT work_unit_id, status, attempts, last_error, failure_stage "
                "FROM work_units WHERE work_unit_id=?",
                (work_unit_id,),
            ).fetchone()
            return self._work_unit(updated)

    def record_failure(
        self,
        work_unit_id: str,
        *,
        error: str,
        retryable: bool,
        failure_stage: str | None = None,
    ) -> None:
        state = "retryable_failed" if retryable else "permanent_failed"
        with self._write():
            updated = self.connection.execute(
                "UPDATE work_units SET status=?, last_error=?, failure_stage=?, terminal_code=? "
                "WHERE work_unit_id=? AND status NOT IN ('done', 'needs_review', 'permanent_failed')",
                (
                    state,
                    error,
                    failure_stage,
                    "provider_retryable" if retryable else "provider_permanent",
                    work_unit_id,
                ),
            ).rowcount
            if updated != 1:
                raise ValueError("work unit cannot record failure from its current state")

    def mark_needs_review(self, work_unit_id: str, *, reason: str) -> None:
        with self._write():
            updated = self.connection.execute(
                "UPDATE work_units SET status='needs_review', last_error=?, "
                "terminal_code='review_required' "
                "WHERE work_unit_id=? AND status IN ('planned', 'running', 'retryable_failed')",
                (reason, work_unit_id),
            ).rowcount
            if updated != 1:
                raise ValueError("work unit cannot transition to needs_review")

    def authorize_cost(self, run_id: str, *, estimated_microusd: int) -> bool:
        if estimated_microusd < 0:
            raise ValueError("estimated cost must be non-negative")
        with self._write():
            run = self.connection.execute(
                "SELECT cost_microusd, cost_limit_microusd, status FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise ValueError("unknown run")
            if run["status"] == "cost_paused":
                return False
            if run["cost_microusd"] + estimated_microusd > run["cost_limit_microusd"]:
                self.connection.execute(
                    "UPDATE runs SET status='cost_paused' WHERE run_id=?",
                    (run_id,),
                )
                return False
            return True

    def record_cost(self, run_id: str, cost_microusd: int) -> None:
        if cost_microusd < 0:
            raise ValueError("cost increment must be non-negative")
        with self._write():
            run = self.connection.execute(
                "SELECT cost_microusd, cost_limit_microusd FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise ValueError("unknown run")
            if run["cost_microusd"] + cost_microusd > run["cost_limit_microusd"]:
                self.connection.execute(
                    "UPDATE runs SET status='cost_paused' WHERE run_id=?",
                    (run_id,),
                )
                raise ValueError("cost increment would exceed the run cap")
            self.connection.execute(
                "UPDATE runs SET cost_microusd=cost_microusd+? WHERE run_id=?",
                (cost_microusd, run_id),
            )

    def mark_done(self, work_unit_id: str, *, result: dict[str, Any] | None = None) -> None:
        result_json = (
            None
            if result is None
            else json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        with self._write():
            updated = self.connection.execute(
                "UPDATE work_units SET status='done', terminal_code='done', result_json=? "
                "WHERE work_unit_id=? AND status IN ('planned', 'running', 'retryable_failed')",
                (result_json, work_unit_id),
            ).rowcount
            if updated != 1:
                raise ValueError("work unit cannot transition to done from its current state")

    def work_unit_result(self, work_unit_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT result_json FROM work_units WHERE work_unit_id=?",
            (work_unit_id,),
        ).fetchone()
        if row is None:
            raise ValueError("unknown work unit")
        return None if row["result_json"] is None else json.loads(row["result_json"])

    def retryable_units(self, run_id: str) -> list[WorkUnit]:
        rows = self.connection.execute(
            "SELECT work_unit_id, status, attempts, last_error, failure_stage "
            "FROM work_units WHERE run_id=? AND status='retryable_failed' "
            "ORDER BY rowid",
            (run_id,),
        ).fetchall()
        return [self._work_unit(row) for row in rows]

    def record_package_checksum(self, run_id: str, checksum: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{64}", checksum):
            raise ValueError("package checksum must be sha256 hex")
        with self._write():
            if (
                self.connection.execute(
                    "UPDATE runs SET package_checksum=? WHERE run_id=?",
                    (checksum, run_id),
                ).rowcount
                != 1
            ):
                raise ValueError("unknown run")

    def summary(self, run_id: str) -> dict[str, int | str | None | bool]:
        with self._write():
            run = self.connection.execute(
                "SELECT * FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise ValueError("unknown run")
            units = self.connection.execute(
                "SELECT status, attempts, last_error FROM work_units WHERE run_id=?",
                (run_id,),
            ).fetchall()
            counts = {
                status: sum(row["status"] == status for row in units)
                for status in (
                    "done",
                    "retryable_failed",
                    "needs_review",
                    "permanent_failed",
                )
            }
            terminal_count = sum(row["status"] in self.TERMINAL_STATUSES for row in units)
            retry_count = sum(row["attempts"] > 1 for row in units)
            error_count = sum(row["last_error"] is not None for row in units)
            all_terminal = bool(units) and terminal_count == len(units)
            if run["status"] == "cost_paused":
                status = "cost_paused"
            elif run["package_checksum"] and all_terminal:
                status = "completed"
            elif counts["retryable_failed"]:
                status = "retrying"
            elif counts["permanent_failed"]:
                status = "failed"
            elif all_terminal:
                status = "awaiting_package"
            else:
                status = "running"
            if status != run["status"]:
                self.connection.execute(
                    "UPDATE runs SET status=? WHERE run_id=?",
                    (status, run_id),
                )
            return {
                "run_id": run_id,
                "status": status,
                "candidate_count": len(units),
                "done_count": counts["done"],
                "retryable_count": counts["retryable_failed"],
                "needs_review_count": counts["needs_review"],
                "permanent_failed_count": counts["permanent_failed"],
                "terminal_count": terminal_count,
                "all_terminal": all_terminal,
                "retry_count": retry_count,
                "error_count": error_count,
                "cost_microusd": run["cost_microusd"],
                "cost_limit_microusd": run["cost_limit_microusd"],
                "package_checksum": run["package_checksum"],
            }
