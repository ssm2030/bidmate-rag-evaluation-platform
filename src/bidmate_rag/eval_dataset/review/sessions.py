"""Eight-hour local review sessions with hashed tokens and double-submit CSRF."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from .db import ReviewDatabase


@dataclass(frozen=True)
class ReviewSession:
    session_id: str
    session_token: str
    csrf_token: str
    created_at: datetime
    expires_at: datetime


class ReviewSessions:
    def __init__(
        self,
        database: ReviewDatabase,
        *,
        now: Callable[[], datetime] | None = None,
        ttl: timedelta = timedelta(hours=8),
    ) -> None:
        self.database = database
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.ttl = ttl

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create(self) -> ReviewSession:
        created_at = self._now()
        expires_at = created_at + self.ttl
        session = ReviewSession(
            session_id=str(uuid4()),
            session_token=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(32),
            created_at=created_at,
            expires_at=expires_at,
        )
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO review_sessions("
                "session_id, session_hash, csrf_hash, created_at, expires_at, last_seen_at, revoked"
                ") VALUES (?, ?, ?, ?, ?, ?, 0)",
                (
                    session.session_id,
                    self._hash(session.session_token),
                    self._hash(session.csrf_token),
                    int(created_at.timestamp()),
                    int(expires_at.timestamp()),
                    int(created_at.timestamp()),
                ),
            )
        return session

    def validate(self, session_token: str, csrf_token: str) -> ReviewSession:
        now = self._now()
        row = self.database.connection.execute(
            "SELECT session_id, session_hash, csrf_hash, created_at, expires_at, revoked "
            "FROM review_sessions WHERE session_hash=?",
            (self._hash(session_token),),
        ).fetchone()
        if row is None:
            raise PermissionError("unknown local review session")
        if row["revoked"]:
            raise PermissionError("revoked local review session")
        if int(now.timestamp()) > row["expires_at"]:
            raise PermissionError("expired local review session")
        if not hmac.compare_digest(row["csrf_hash"], self._hash(csrf_token)):
            raise PermissionError("csrf token mismatch")
        self.database.connection.execute(
            "UPDATE review_sessions SET last_seen_at=? WHERE session_id=?",
            (int(now.timestamp()), row["session_id"]),
        )
        return ReviewSession(
            session_id=row["session_id"],
            session_token=session_token,
            csrf_token=csrf_token,
            created_at=datetime.fromtimestamp(row["created_at"], tz=timezone.utc),
            expires_at=datetime.fromtimestamp(row["expires_at"], tz=timezone.utc),
        )

    def revoke(self, session_token: str) -> None:
        updated = self.database.connection.execute(
            "UPDATE review_sessions SET revoked=1 WHERE session_hash=?",
            (self._hash(session_token),),
        ).rowcount
        if updated != 1:
            raise PermissionError("unknown local review session")
