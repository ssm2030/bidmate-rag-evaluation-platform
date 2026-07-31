from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Literal
from uuid import uuid4


@dataclass(frozen=True)
class WorkUnit:
    work_unit_id: str
    status: str
    attempts: int = 0
    last_error: str | None = None
    failure_stage: str | None = None


class CostLimitExceeded(RuntimeError):
    """Raised before a provider request would exceed a campaign hard cap."""


class OperationalCostCapExceeded(CostLimitExceeded):
    """Raised inside the reservation transaction when the 4.50 USD call ceiling is reached."""

@dataclass(frozen=True)
class ProviderCall:
    provider_call_id: str
    run_id: str
    work_unit_id: str
    stage: str
    attempt: int
    model: str
    request_hash: str
    reserved_microusd: int
    status: str


@dataclass(frozen=True)
class CostTotals:
    actual_microusd: int
    open_reserved_microusd: int
    effective_microusd: int
    cost_limit_microusd: int


class AutomationLedger:
    """SQLite-backed local automation state with idempotent live-cost reservations."""

    TERMINAL_STATUSES = frozenset({"done", "needs_review", "permanent_failed"})
    _OPEN_CALL_STATUSES = ("reserved", "unknown")

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

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

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
                "campaign_id": "TEXT REFERENCES live_campaigns(campaign_id)",
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
            "CREATE TABLE IF NOT EXISTS live_campaigns ("
            "campaign_id TEXT PRIMARY KEY, campaign_key TEXT NOT NULL UNIQUE, "
            "cost_limit_microusd INTEGER NOT NULL CHECK (cost_limit_microusd >= 0), "
            "created_at TEXT NOT NULL)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS provider_calls ("
            "provider_call_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, work_unit_id TEXT NOT NULL, "
            "stage TEXT NOT NULL, attempt INTEGER NOT NULL, model TEXT NOT NULL, "
            "request_hash TEXT NOT NULL, reserved_microusd INTEGER NOT NULL "
            "CHECK (reserved_microusd >= 0), actual_microusd INTEGER "
            "CHECK (actual_microusd >= 0), input_tokens INTEGER, output_tokens INTEGER, "
            "provider_response_id TEXT, status TEXT NOT NULL CHECK (status IN "
            "('reserved', 'succeeded', 'released', 'unknown', 'failed')), error_code TEXT, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "FOREIGN KEY (run_id) REFERENCES runs(run_id), "
            "UNIQUE (work_unit_id, stage, attempt, request_hash))"
        )
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_work_units_run_id ON work_units(run_id)")
        self.connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_identity_hash "
            "ON runs(identity_hash) WHERE identity_hash IS NOT NULL"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_provider_calls_run_id ON provider_calls(run_id)"
        )

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        known = {row[1] for row in self.connection.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in known:
                self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    @staticmethod
    def _work_unit(row: sqlite3.Row) -> WorkUnit:
        return WorkUnit(
            row["work_unit_id"], row["status"], row["attempts"], row["last_error"], row["failure_stage"]
        )

    @staticmethod
    def _provider_call(row: sqlite3.Row) -> ProviderCall:
        return ProviderCall(
            provider_call_id=str(row["provider_call_id"]),
            run_id=str(row["run_id"]),
            work_unit_id=str(row["work_unit_id"]),
            stage=str(row["stage"]),
            attempt=int(row["attempt"]),
            model=str(row["model"]),
            request_hash=str(row["request_hash"]),
            reserved_microusd=int(row["reserved_microusd"]),
            status=str(row["status"]),
        )

    def create_campaign(self, *, campaign_key: str, cost_limit_microusd: int) -> str:
        if not campaign_key:
            raise ValueError("campaign key is required")
        if cost_limit_microusd < 0:
            raise ValueError("cost limit must be non-negative")
        with self._write():
            existing = self.connection.execute(
                "SELECT campaign_id, cost_limit_microusd FROM live_campaigns WHERE campaign_key=?",
                (campaign_key,),
            ).fetchone()
            if existing:
                if int(existing["cost_limit_microusd"]) != cost_limit_microusd:
                    raise ValueError("campaign cost limit is immutable")
                return str(existing["campaign_id"])
            campaign_id = str(uuid4())
            self.connection.execute(
                "INSERT INTO live_campaigns (campaign_id, campaign_key, cost_limit_microusd, created_at) "
                "VALUES (?, ?, ?, ?)",
                (campaign_id, campaign_key, cost_limit_microusd, self._now()),
            )
            return campaign_id

    def create_run(
        self,
        dataset_id: str | None = None,
        *,
        cost_limit_microusd: int,
        run_id: str | None = None,
        identity_hash: str | None = None,
        identity_json: str = "{}",
        mode: str = "mock",
        campaign_id: str | None = None,
        run_key: str | None = None,
    ) -> str:
        if cost_limit_microusd < 0:
            raise ValueError("cost limit must be non-negative")
        if mode not in {"mock", "live"}:
            raise ValueError("mode must be mock or live")
        dataset_id = dataset_id or run_key
        if not dataset_id:
            raise ValueError("dataset_id or run_key is required")
        if mode == "live" and not campaign_id:
            raise ValueError("live runs require a campaign")
        run_id = run_id or str(uuid4())
        with self._write():
            if campaign_id:
                campaign = self.connection.execute(
                    "SELECT cost_limit_microusd FROM live_campaigns WHERE campaign_id=?", (campaign_id,)
                ).fetchone()
                if campaign is None:
                    raise ValueError("unknown campaign")
                if cost_limit_microusd != int(campaign["cost_limit_microusd"]):
                    raise ValueError("run cost limit must equal campaign cost limit")
            self.connection.execute(
                "INSERT INTO runs (run_id, dataset_id, cost_limit_microusd, status, cost_microusd, "
                "created_at, identity_hash, identity_json, mode, campaign_id) "
                "VALUES (?, ?, ?, 'running', 0, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    dataset_id,
                    cost_limit_microusd,
                    self._now(),
                    identity_hash,
                    identity_json,
                    mode,
                    campaign_id,
                ),
            )
        return run_id

    @staticmethod
    def identity_hash(identity: dict[str, Any]) -> tuple[str, str]:
        identity_json = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return identity_json, hashlib.sha256(identity_json.encode("utf-8")).hexdigest()

    def run_id_for_identity(self, identity: dict[str, Any]) -> str | None:
        _, identity_hash = self.identity_hash(identity)
        row = self.connection.execute(
            "SELECT run_id FROM runs WHERE identity_hash=? LIMIT 1", (identity_hash,)
        ).fetchone()
        return None if row is None else str(row["run_id"])

    def get_or_create_run_for_identity(
        self, *, dataset_id: str, identity: dict[str, Any], cost_limit_microusd: int
    ) -> str:
        identity_json, identity_hash = self.identity_hash(identity)
        row = self.connection.execute(
            "SELECT run_id FROM runs WHERE identity_hash=? LIMIT 1", (identity_hash,)
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
            "SELECT run_id FROM runs WHERE dataset_id=? ORDER BY created_at DESC LIMIT 1", (dataset_id,)
        ).fetchone()
        return str(row["run_id"]) if row else self.create_run(dataset_id, cost_limit_microusd=cost_limit_microusd)

    def run_id_for_dataset(self, dataset_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT run_id FROM runs WHERE dataset_id=? ORDER BY created_at DESC LIMIT 1", (dataset_id,)
        ).fetchone()
        return None if row is None else str(row["run_id"])

    def _campaign_totals(self, campaign_id: str) -> CostTotals:
        campaign = self.connection.execute(
            "SELECT cost_limit_microusd FROM live_campaigns WHERE campaign_id=?", (campaign_id,)
        ).fetchone()
        if campaign is None:
            raise ValueError("unknown campaign")
        totals = self.connection.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN pc.status='succeeded' THEN pc.actual_microusd ELSE 0 END), 0) AS actual, "
            "COALESCE(SUM(CASE WHEN pc.status IN ('reserved', 'unknown') THEN pc.reserved_microusd ELSE 0 END), 0) AS open "
            "FROM provider_calls pc JOIN runs r ON r.run_id=pc.run_id WHERE r.campaign_id=?",
            (campaign_id,),
        ).fetchone()
        actual = int(totals["actual"])
        open_reserved = int(totals["open"])
        return CostTotals(
            actual_microusd=actual,
            open_reserved_microusd=open_reserved,
            effective_microusd=actual + open_reserved,
            cost_limit_microusd=int(campaign["cost_limit_microusd"]),
        )

    def reserve_provider_call(
        self,
        *,
        run_id: str,
        work_unit_id: str,
        stage: str,
        attempt: int,
        model: str,
        request_hash: str,
        reserved_microusd: int,
        operational_cap_microusd: int | None = None,
    ) -> ProviderCall:
        if not all((work_unit_id, stage, model)):
            raise ValueError("provider call identity fields are required")
        if attempt < 1 or reserved_microusd < 0:
            raise ValueError("attempt and reserved cost must be non-negative")
        if operational_cap_microusd is not None and operational_cap_microusd < 0:
            raise ValueError("operational cap must be non-negative")
        if not re.fullmatch(r"[a-f0-9]{64}", request_hash):
            raise ValueError("request_hash must be sha256 hex")
        with self._write():
            existing = self.connection.execute(
                "SELECT * FROM provider_calls WHERE work_unit_id=? AND stage=? AND attempt=? AND request_hash=?",
                (work_unit_id, stage, attempt, request_hash),
            ).fetchone()
            if existing is not None:
                return self._provider_call(existing)
            run = self.connection.execute(
                "SELECT campaign_id, mode FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise ValueError("unknown run")
            if run["mode"] != "live" or run["campaign_id"] is None:
                raise ValueError("provider calls require a live campaign run")
            totals = self._campaign_totals(str(run["campaign_id"]))
            if (
                operational_cap_microusd is not None
                and totals.effective_microusd + reserved_microusd > operational_cap_microusd
            ):
                self.connection.execute("UPDATE runs SET status='cost_paused' WHERE run_id=?", (run_id,))
                raise OperationalCostCapExceeded("provider reservation would exceed operational cost cap")
            if totals.effective_microusd + reserved_microusd > totals.cost_limit_microusd:
                self.connection.execute("UPDATE runs SET status='cost_paused' WHERE run_id=?", (run_id,))
                raise CostLimitExceeded("provider reservation would exceed campaign cost limit")
            provider_call_id = str(uuid4())
            now = self._now()
            self.connection.execute(
                "INSERT INTO provider_calls (provider_call_id, run_id, work_unit_id, stage, attempt, model, "
                "request_hash, reserved_microusd, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?)",
                (
                    provider_call_id,
                    run_id,
                    work_unit_id,
                    stage,
                    attempt,
                    model,
                    request_hash,
                    reserved_microusd,
                    now,
                    now,
                ),
            )
            return ProviderCall(
                provider_call_id, run_id, work_unit_id, stage, attempt, model, request_hash, reserved_microusd, "reserved"
            )

    def reconcile_provider_call(
        self,
        *,
        provider_call_id: str,
        status: Literal["succeeded", "released", "failed"],
        actual_microusd: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        provider_response_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        if status == "succeeded":
            if actual_microusd is None or actual_microusd < 0:
                raise ValueError("successful calls require a non-negative actual cost")
            if input_tokens is None or input_tokens < 0 or output_tokens is None or output_tokens < 0:
                raise ValueError("successful calls require non-negative token usage")
        with self._write():
            call = self.connection.execute(
                "SELECT reserved_microusd, status FROM provider_calls WHERE provider_call_id=?",
                (provider_call_id,),
            ).fetchone()
            if call is None:
                raise ValueError("unknown provider call")
            if call["status"] != "reserved":
                raise ValueError("provider call is not pending reconciliation")
            if status == "succeeded" and int(actual_microusd or 0) > int(call["reserved_microusd"]):
                raise ValueError("actual cost exceeds prior reservation")
            self.connection.execute(
                "UPDATE provider_calls SET status=?, actual_microusd=?, input_tokens=?, output_tokens=?, "
                "provider_response_id=?, error_code=?, updated_at=? WHERE provider_call_id=?",
                (
                    status,
                    actual_microusd if status == "succeeded" else None,
                    input_tokens if status == "succeeded" else None,
                    output_tokens if status == "succeeded" else None,
                    provider_response_id if status == "succeeded" else None,
                    error_code,
                    self._now(),
                    provider_call_id,
                ),
            )

    def mark_provider_call_unknown(self, provider_call_id: str, *, error_code: str) -> None:
        if not error_code:
            raise ValueError("error code is required")
        with self._write():
            updated = self.connection.execute(
                "UPDATE provider_calls SET status='unknown', error_code=?, updated_at=? "
                "WHERE provider_call_id=? AND status='reserved'",
                (error_code, self._now(), provider_call_id),
            ).rowcount
            if updated != 1:
                raise ValueError("provider call cannot become unknown from its current state")

    def recover_captured_selector_response(
        self,
        *,
        provider_call_id: str,
        actual_microusd: int,
        input_tokens: int,
        output_tokens: int,
        provider_response_id: str,
    ) -> WorkUnit:
        if actual_microusd < 0 or input_tokens < 0 or output_tokens < 0:
            raise ValueError("captured response usage must be non-negative")
        if not provider_response_id:
            raise ValueError("captured response id is required")
        with self._write():
            row = self.connection.execute(
                "SELECT pc.work_unit_id, pc.stage, pc.status AS call_status, "
                "pc.error_code, pc.reserved_microusd, pc.actual_microusd, "
                "pc.input_tokens, pc.output_tokens, pc.provider_response_id, "
                "wu.status AS unit_status, wu.last_error, wu.failure_stage, "
                "wu.terminal_code "
                "FROM provider_calls pc JOIN work_units wu "
                "ON wu.work_unit_id=pc.work_unit_id WHERE pc.provider_call_id=?",
                (provider_call_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown provider call")
            resumable_state = (
                row["unit_status"] in {"retryable_failed", "running"}
                and row["last_error"] == "captured_provider_response_recovered"
                and row["terminal_code"] is None
            )
            downstream_generator_failure = (
                row["unit_status"] == "permanent_failed"
                and isinstance(row["last_error"], str)
                and row["last_error"].startswith("provider_output_repair:")
                and row["terminal_code"] == "provider_permanent"
            )
            already_recovered = (
                row["stage"] == "selector"
                and row["call_status"] == "succeeded"
                and row["error_code"] == "captured_response_recovered"
                and row["failure_stage"] == "generator"
                and (resumable_state or downstream_generator_failure)
            )
            if already_recovered:
                if (
                    int(row["actual_microusd"]) != actual_microusd
                    or int(row["input_tokens"]) != input_tokens
                    or int(row["output_tokens"]) != output_tokens
                    or str(row["provider_response_id"]) != provider_response_id
                ):
                    raise ValueError("captured selector replay does not match prior recovery")
                current = self.connection.execute(
                    "SELECT work_unit_id, status, attempts, last_error, failure_stage "
                    "FROM work_units WHERE work_unit_id=?",
                    (str(row["work_unit_id"]),),
                ).fetchone()
                return self._work_unit(current)
            if (
                row["stage"] != "selector"
                or row["call_status"] != "unknown"
                or row["error_code"] != "invalid_response"
                or row["unit_status"] != "permanent_failed"
                or row["failure_stage"] != "selector"
                or row["terminal_code"] != "provider_permanent"
            ):
                raise ValueError("captured selector response is not recoverable")
            if actual_microusd > int(row["reserved_microusd"]):
                raise ValueError("actual cost exceeds prior reservation")
            self.connection.execute(
                "UPDATE provider_calls SET status='succeeded', actual_microusd=?, "
                "input_tokens=?, output_tokens=?, provider_response_id=?, "
                "error_code='captured_response_recovered', updated_at=? "
                "WHERE provider_call_id=?",
                (
                    actual_microusd,
                    input_tokens,
                    output_tokens,
                    provider_response_id,
                    self._now(),
                    provider_call_id,
                ),
            )
            self.connection.execute(
                "UPDATE work_units SET status='retryable_failed', "
                "last_error='captured_provider_response_recovered', "
                "failure_stage='generator', terminal_code=NULL "
                "WHERE work_unit_id=?",
                (str(row["work_unit_id"]),),
            )
            updated = self.connection.execute(
                "SELECT work_unit_id, status, attempts, last_error, failure_stage "
                "FROM work_units WHERE work_unit_id=?",
                (str(row["work_unit_id"]),),
            ).fetchone()
            return self._work_unit(updated)
    def reopen_captured_generator_response(
        self,
        *,
        provider_call_id: str,
        actual_microusd: int,
        input_tokens: int,
        output_tokens: int,
        provider_response_id: str,
    ) -> WorkUnit:
        if actual_microusd < 0 or input_tokens < 0 or output_tokens < 0:
            raise ValueError("captured response usage must be non-negative")
        if not provider_response_id:
            raise ValueError("captured response id is required")
        with self._write():
            row = self.connection.execute(
                "SELECT pc.work_unit_id, pc.stage, pc.status AS call_status, "
                "pc.error_code, pc.actual_microusd, pc.input_tokens, "
                "pc.output_tokens, pc.provider_response_id, "
                "wu.status AS unit_status, wu.attempts, wu.last_error, "
                "wu.failure_stage, wu.terminal_code "
                "FROM provider_calls pc JOIN work_units wu "
                "ON wu.work_unit_id=pc.work_unit_id WHERE pc.provider_call_id=?",
                (provider_call_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown provider call")
            values_match = (
                row["actual_microusd"] is not None
                and int(row["actual_microusd"]) == actual_microusd
                and row["input_tokens"] is not None
                and int(row["input_tokens"]) == input_tokens
                and row["output_tokens"] is not None
                and int(row["output_tokens"]) == output_tokens
                and str(row["provider_response_id"]) == provider_response_id
            )
            already_reopened = (
                row["stage"] == "generator"
                and row["call_status"] == "reserved"
                and row["error_code"] == "captured_generator_replay"
                and row["unit_status"] == "running"
                and int(row["attempts"]) == 3
                and row["last_error"] == "captured_generator_response_replay"
                and row["failure_stage"] == "generator"
                and row["terminal_code"] is None
            )
            if already_reopened:
                if not values_match:
                    raise ValueError("captured generator replay does not match prior response")
                current = self.connection.execute(
                    "SELECT work_unit_id, status, attempts, last_error, failure_stage "
                    "FROM work_units WHERE work_unit_id=?",
                    (str(row["work_unit_id"]),),
                ).fetchone()
                return self._work_unit(current)
            reviewer_calls = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM provider_calls "
                    "WHERE work_unit_id=? AND stage='reviewer'",
                    (str(row["work_unit_id"]),),
                ).fetchone()[0]
            )
            if (
                row["stage"] != "generator"
                or row["call_status"] != "succeeded"
                or row["error_code"] != "invalid_response"
                or not values_match
                or row["unit_status"] != "permanent_failed"
                or int(row["attempts"]) != 3
                or row["failure_stage"] != "generator"
                or row["terminal_code"] != "provider_permanent"
                or reviewer_calls != 0
            ):
                raise ValueError("captured generator response is not recoverable")
            self.connection.execute(
                "UPDATE provider_calls SET status='reserved', "
                "error_code='captured_generator_replay', updated_at=? "
                "WHERE provider_call_id=?",
                (self._now(), provider_call_id),
            )
            self.connection.execute(
                "UPDATE work_units SET status='running', "
                "last_error='captured_generator_response_replay', "
                "failure_stage='generator', terminal_code=NULL "
                "WHERE work_unit_id=?",
                (str(row["work_unit_id"]),),
            )
            updated = self.connection.execute(
                "SELECT work_unit_id, status, attempts, last_error, failure_stage "
                "FROM work_units WHERE work_unit_id=?",
                (str(row["work_unit_id"]),),
            ).fetchone()
            return self._work_unit(updated)

    def complete_captured_generator_replay(self, provider_call_id: str) -> WorkUnit:
        with self._write():
            row = self.connection.execute(
                "SELECT pc.work_unit_id, pc.stage, pc.status AS call_status, "
                "pc.error_code, wu.status AS unit_status, wu.attempts, "
                "wu.last_error, wu.failure_stage, wu.terminal_code "
                "FROM provider_calls pc JOIN work_units wu "
                "ON wu.work_unit_id=pc.work_unit_id WHERE pc.provider_call_id=?",
                (provider_call_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown provider call")
            already_completed = (
                row["stage"] == "generator"
                and row["call_status"] == "succeeded"
                and row["error_code"] == "captured_generator_response_recovered"
                and row["unit_status"] in {"retryable_failed", "running"}
                and row["last_error"] == "captured_generator_response_recovered"
                and row["failure_stage"] == "reviewer"
                and row["terminal_code"] is None
            )
            if already_completed:
                current = self.connection.execute(
                    "SELECT work_unit_id, status, attempts, last_error, failure_stage "
                    "FROM work_units WHERE work_unit_id=?",
                    (str(row["work_unit_id"]),),
                ).fetchone()
                return self._work_unit(current)
            if (
                row["stage"] != "generator"
                or row["call_status"] != "succeeded"
                or row["error_code"] is not None
                or row["unit_status"] != "running"
                or int(row["attempts"]) != 3
                or row["last_error"] != "captured_generator_response_replay"
                or row["failure_stage"] != "generator"
                or row["terminal_code"] is not None
            ):
                raise ValueError("captured generator replay did not complete safely")
            self.connection.execute(
                "UPDATE provider_calls SET "
                "error_code='captured_generator_response_recovered', updated_at=? "
                "WHERE provider_call_id=?",
                (self._now(), provider_call_id),
            )
            self.connection.execute(
                "UPDATE work_units SET status='retryable_failed', attempts=2, "
                "last_error='captured_generator_response_recovered', "
                "failure_stage='reviewer', terminal_code=NULL "
                "WHERE work_unit_id=?",
                (str(row["work_unit_id"]),),
            )
            updated = self.connection.execute(
                "SELECT work_unit_id, status, attempts, last_error, failure_stage "
                "FROM work_units WHERE work_unit_id=?",
                (str(row["work_unit_id"]),),
            ).fetchone()
            return self._work_unit(updated)

    def rollback_unstarted_captured_resume(self, work_unit_id: str) -> WorkUnit:
        with self._write():
            updated_count = self.connection.execute(
                "UPDATE work_units SET status=?, attempts=? "
                "WHERE work_unit_id=? AND status=? AND attempts=? "
                "AND last_error=? AND failure_stage=? "
                "AND NOT EXISTS ("
                "SELECT 1 FROM provider_calls WHERE work_unit_id=? AND stage IN (?, ?)"
                ")",
                (
                    "retryable_failed",
                    2,
                    work_unit_id,
                    "running",
                    3,
                    "captured_provider_response_recovered",
                    "generator",
                    work_unit_id,
                    "generator",
                    "reviewer",
                ),
            ).rowcount
            if updated_count != 1:
                raise ValueError("captured resume claim cannot be rolled back")
            updated = self.connection.execute(
                "SELECT work_unit_id, status, attempts, last_error, failure_stage "
                "FROM work_units WHERE work_unit_id=?",
                (work_unit_id,),
            ).fetchone()
            return self._work_unit(updated)

    def provider_call(self, provider_call_id: str) -> ProviderCall:
        row = self.connection.execute(
            "SELECT * FROM provider_calls WHERE provider_call_id=?", (provider_call_id,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown provider call")
        return self._provider_call(row)

    def provider_call_retryable(self, provider_call_id: str) -> bool:
        row = self.connection.execute(
            "SELECT status FROM provider_calls WHERE provider_call_id=?", (provider_call_id,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown provider call")
        return str(row["status"]) == "released"

    def pause_run_for_cost(self, run_id: str) -> None:
        with self._write():
            updated = self.connection.execute(
                "UPDATE runs SET status='cost_paused' WHERE run_id=?", (run_id,)
            ).rowcount
            if updated != 1:
                raise ValueError("unknown run")
    def get_cost_totals(self, run_id: str) -> CostTotals:
        run = self.connection.execute("SELECT campaign_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise ValueError("unknown run")
        if run["campaign_id"] is None:
            raise ValueError("run has no live campaign")
        return self._campaign_totals(str(run["campaign_id"]))

    def create_work_unit(
        self, run_id: str, *, ordinal: int, plan: dict[str, Any], prompt_bundle_hash: str
    ) -> WorkUnit:
        plan_json = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        key = hashlib.sha256(f"{run_id}\0{ordinal}\0{plan_json}\0{prompt_bundle_hash}".encode()).hexdigest()
        with self._write():
            row = self.connection.execute(
                "SELECT work_unit_id, status, attempts, last_error, failure_stage FROM work_units "
                "WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if row:
                return self._work_unit(row)
            work_unit_id = str(uuid4())
            self.connection.execute(
                "INSERT INTO work_units (work_unit_id, run_id, idempotency_key, status, attempts) "
                "VALUES (?, ?, ?, 'planned', 0)",
                (work_unit_id, run_id, key),
            )
            return WorkUnit(work_unit_id, "planned")

    def claim(self, work_unit_id: str) -> WorkUnit:
        with self._write():
            row = self.connection.execute(
                "SELECT w.work_unit_id, w.status, w.attempts, w.last_error, w.failure_stage, "
                "r.status AS run_status FROM work_units w JOIN runs r ON r.run_id=w.run_id "
                "WHERE w.work_unit_id=?",
                (work_unit_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown work unit")
            if row["run_status"] == "cost_paused" and row["status"] not in self.TERMINAL_STATUSES:
                return self._work_unit(row)
            if row["status"] in {"planned", "retryable_failed"}:
                self.connection.execute(
                    "UPDATE work_units SET status='running', attempts=attempts+1 WHERE work_unit_id=?",
                    (work_unit_id,),
                )
            updated = self.connection.execute(
                "SELECT work_unit_id, status, attempts, last_error, failure_stage FROM work_units "
                "WHERE work_unit_id=?",
                (work_unit_id,),
            ).fetchone()
            return self._work_unit(updated)

    def record_failure(
        self, work_unit_id: str, *, error: str, retryable: bool, failure_stage: str | None = None
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
                "UPDATE work_units SET status='needs_review', last_error=?, terminal_code='review_required' "
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
                "SELECT cost_microusd, cost_limit_microusd, status FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise ValueError("unknown run")
            if run["status"] == "cost_paused":
                return False
            if int(run["cost_microusd"]) + estimated_microusd > int(run["cost_limit_microusd"]):
                self.connection.execute("UPDATE runs SET status='cost_paused' WHERE run_id=?", (run_id,))
                return False
            return True

    def record_cost(self, run_id: str, cost_microusd: int) -> None:
        if cost_microusd < 0:
            raise ValueError("cost increment must be non-negative")
        with self._write():
            run = self.connection.execute(
                "SELECT cost_microusd, cost_limit_microusd FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise ValueError("unknown run")
            if int(run["cost_microusd"]) + cost_microusd > int(run["cost_limit_microusd"]):
                self.connection.execute("UPDATE runs SET status='cost_paused' WHERE run_id=?", (run_id,))
                raise ValueError("cost increment would exceed the run cap")
            self.connection.execute(
                "UPDATE runs SET cost_microusd=cost_microusd+? WHERE run_id=?", (cost_microusd, run_id)
            )

    def mark_done(self, work_unit_id: str, *, result: dict[str, Any] | None = None) -> None:
        result_json = None if result is None else json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
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
            "SELECT result_json FROM work_units WHERE work_unit_id=?", (work_unit_id,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown work unit")
        return None if row["result_json"] is None else json.loads(row["result_json"])

    def requeue_released_auth_failures(self, run_id: str, *, limit: int = 1) -> list[WorkUnit]:
        if limit < 1 or limit > 5:
            raise ValueError("limit must be between 1 and 5")
        auth_error_codes = (
            "selector_provider_http_401",
            "generator_provider_http_401",
            "reviewer_provider_http_401",
        )
        with self._write():
            rows = self.connection.execute(
                "SELECT wu.work_unit_id, wu.status, wu.attempts, wu.last_error, wu.failure_stage "
                "FROM work_units wu "
                "WHERE wu.run_id=? AND wu.status='permanent_failed' "
                "AND wu.terminal_code='provider_permanent' "
                "AND wu.last_error IN (?, ?, ?) "
                "AND EXISTS ("
                "SELECT 1 FROM provider_calls pc "
                "WHERE pc.work_unit_id=wu.work_unit_id "
                "AND pc.status='released' AND pc.error_code=wu.last_error"
                ") "
                "ORDER BY wu.rowid LIMIT ?",
                (run_id, *auth_error_codes, limit),
            ).fetchall()
            work_unit_ids = [str(row["work_unit_id"]) for row in rows]
            for work_unit_id in work_unit_ids:
                self.connection.execute(
                    "UPDATE work_units SET status='retryable_failed', terminal_code=NULL "
                    "WHERE work_unit_id=? AND status='permanent_failed'",
                    (work_unit_id,),
                )
            if not work_unit_ids:
                return []
            placeholders = ",".join("?" for _ in work_unit_ids)
            updated = self.connection.execute(
                "SELECT work_unit_id, status, attempts, last_error, failure_stage "
                f"FROM work_units WHERE work_unit_id IN ({placeholders}) ORDER BY rowid",
                work_unit_ids,
            ).fetchall()
            return [self._work_unit(row) for row in updated]

    def requeue_post_auth_contract_failures(
        self, run_id: str, *, limit: int = 2
    ) -> list[WorkUnit]:
        if limit < 1 or limit > 2:
            raise ValueError("limit must be between 1 and 2")
        with self._write():
            rows = self.connection.execute(
                "SELECT wu.work_unit_id, wu.last_error, "
                "(SELECT COUNT(*) FROM provider_calls selected "
                "WHERE selected.work_unit_id=wu.work_unit_id "
                "AND selected.stage='selector' AND selected.attempt=2 "
                "AND selected.status='succeeded') AS successful_selector_count "
                "FROM work_units wu "
                "WHERE wu.run_id=? AND wu.status='permanent_failed' "
                "AND wu.attempts=2 AND wu.failure_stage='generator' "
                "AND wu.terminal_code='provider_permanent' "
                "AND wu.last_error LIKE 'provider_output_repair:%' "
                "AND EXISTS ("
                "SELECT 1 FROM provider_calls auth "
                "WHERE auth.work_unit_id=wu.work_unit_id "
                "AND auth.stage='selector' AND auth.attempt=1 "
                "AND auth.status='released' "
                "AND auth.error_code='selector_provider_http_401'"
                ") "
                "AND EXISTS ("
                "SELECT 1 FROM provider_calls generated "
                "WHERE generated.work_unit_id=wu.work_unit_id "
                "AND generated.stage='generator' AND generated.attempt=2 "
                "AND generated.status='succeeded'"
                ") "
                "AND NOT EXISTS ("
                "SELECT 1 FROM provider_calls reviewed "
                "WHERE reviewed.work_unit_id=wu.work_unit_id "
                "AND reviewed.stage='reviewer'"
                ") "
                "ORDER BY wu.rowid LIMIT ?",
                (run_id, limit),
            ).fetchall()
            work_unit_ids = [str(row["work_unit_id"]) for row in rows]
            for row in rows:
                is_multi_document = (
                    "multi document_scope requires" in str(row["last_error"])
                )
                selector_repair_complete = (
                    is_multi_document
                    and int(row["successful_selector_count"]) >= 2
                )
                failure_stage = (
                    "generator"
                    if selector_repair_complete or not is_multi_document
                    else "selector"
                )
                if selector_repair_complete:
                    repair_reason = (
                        "multi_document_generator_requires_two_documents"
                    )
                elif is_multi_document:
                    repair_reason = (
                        "multi_document_scope_requires_two_documents"
                    )
                else:
                    repair_reason = "type_d_requires_zero_evidence_claims"
                repair_error = (
                    f"provider_output_repair:{repair_reason}:{row['last_error']}"
                )
                self.connection.execute(
                    "UPDATE work_units SET status='retryable_failed', attempts=1, "
                    "last_error=?, failure_stage=?, terminal_code=NULL "
                    "WHERE work_unit_id=? AND status='permanent_failed'",
                    (
                        repair_error,
                        failure_stage,
                        str(row["work_unit_id"]),
                    ),
                )
            if not work_unit_ids:
                return []
            placeholders = ",".join("?" for _ in work_unit_ids)
            updated = self.connection.execute(
                "SELECT work_unit_id, status, attempts, last_error, failure_stage "
                f"FROM work_units WHERE work_unit_id IN ({placeholders}) ORDER BY rowid",
                work_unit_ids,
            ).fetchall()
            return [self._work_unit(row) for row in updated]

    def retryable_units(self, run_id: str) -> list[WorkUnit]:
        rows = self.connection.execute(
            "SELECT work_unit_id, status, attempts, last_error, failure_stage FROM work_units "
            "WHERE run_id=? AND status='retryable_failed' ORDER BY rowid",
            (run_id,),
        ).fetchall()
        return [self._work_unit(row) for row in rows]

    def record_package_checksum(self, run_id: str, checksum: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{64}", checksum):
            raise ValueError("package checksum must be sha256 hex")
        with self._write():
            if self.connection.execute(
                "UPDATE runs SET package_checksum=? WHERE run_id=?", (checksum, run_id)
            ).rowcount != 1:
                raise ValueError("unknown run")

    def summary(self, run_id: str) -> dict[str, int | str | None | bool]:
        with self._write():
            run = self.connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise ValueError("unknown run")
            units = self.connection.execute(
                "SELECT status, attempts, last_error FROM work_units WHERE run_id=?", (run_id,)
            ).fetchall()
            counts = {
                status: sum(row["status"] == status for row in units)
                for status in ("done", "retryable_failed", "needs_review", "permanent_failed")
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
                self.connection.execute("UPDATE runs SET status=? WHERE run_id=?", (status, run_id))
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
