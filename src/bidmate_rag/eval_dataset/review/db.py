"""Small SQLite boundary for the local review workstation."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class ReviewConflictError(RuntimeError):
    """The editor attempted to save a stale revision."""


class ApprovalBlockedError(RuntimeError):
    """An item cannot be approved or exported until reviewer gates pass."""


class _SerializedCursor(sqlite3.Cursor):
    """Serialize every SQLite cursor call made through the shared local connection."""

    @property
    def _operation_lock(self) -> threading.RLock:
        connection = self.connection
        if not isinstance(connection, _SerializedConnection):
            raise TypeError("serialized cursor requires a serialized connection")
        return connection.operation_lock

    def execute(self, sql: str, parameters=(), /):
        with self._operation_lock:
            return super().execute(sql, parameters)

    def executemany(self, sql: str, seq_of_parameters, /):
        with self._operation_lock:
            return super().executemany(sql, seq_of_parameters)

    def executescript(self, sql_script: str, /):
        with self._operation_lock:
            return super().executescript(sql_script)

    def fetchone(self):
        with self._operation_lock:
            return super().fetchone()

    def fetchmany(self, size: int | None = None):
        with self._operation_lock:
            return super().fetchmany() if size is None else super().fetchmany(size)

    def fetchall(self):
        with self._operation_lock:
            return super().fetchall()


class _SerializedConnection(sqlite3.Connection):
    """A single SQLite connection safe for FastAPI's concurrent worker threads."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.operation_lock = threading.RLock()

    def cursor(self, factory=None):
        with self.operation_lock:
            return super().cursor(factory or _SerializedCursor)

    def execute(self, sql: str, parameters=(), /):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql: str, seq_of_parameters, /):
        return self.cursor().executemany(sql, seq_of_parameters)

    def executescript(self, sql_script: str, /):
        return self.cursor().executescript(sql_script)


class ReviewDatabase:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            timeout=5,
            isolation_level=None,
            factory=_SerializedConnection,
        )
        self.connection.row_factory = sqlite3.Row
        self._lock = self.connection.operation_lock
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self._apply_migrations()

    def _apply_migrations(self) -> None:
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS review_schema_migrations ("
            "version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        migration_root = Path(__file__).parent / "migrations"
        for migration in sorted(migration_root.glob("*.sql")):
            payload = migration.read_bytes()
            checksum = hashlib.sha256(payload).hexdigest()
            version = migration.stem
            existing = self.connection.execute(
                "SELECT checksum FROM review_schema_migrations WHERE version=?",
                (version,),
            ).fetchone()
            if existing is not None:
                if existing["checksum"] != checksum:
                    raise RuntimeError(
                        f"review database migration checksum changed: {version}; use a backup and a new migration"
                    )
                continue
            self.connection.executescript(payload.decode("utf-8"))
            self.connection.execute(
                "INSERT INTO review_schema_migrations(version, checksum) VALUES (?, ?)",
                (version, checksum),
            )
        self.connection.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except Exception:
                self.connection.execute("ROLLBACK")
                raise
            else:
                self.connection.execute("COMMIT")
