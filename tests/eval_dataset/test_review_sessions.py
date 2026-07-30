from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bidmate_rag.eval_dataset.review.db import ReviewDatabase
from bidmate_rag.eval_dataset.review.sessions import ReviewSessions


def test_session_tokens_are_hashed_expire_after_eight_hours_and_can_be_revoked(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 30, 0, 0, tzinfo=timezone.utc)
    current = [now]
    database = ReviewDatabase(tmp_path / "review.sqlite3")
    sessions = ReviewSessions(database, now=lambda: current[0])

    created = sessions.create()
    assert created.expires_at == now + timedelta(hours=8)
    assert (
        sessions.validate(created.session_token, created.csrf_token).session_id
        == created.session_id
    )
    row = database.connection.execute(
        "SELECT session_hash, csrf_hash FROM review_sessions"
    ).fetchone()
    assert created.session_token not in tuple(row)
    assert created.csrf_token not in tuple(row)

    with pytest.raises(PermissionError, match="csrf"):
        sessions.validate(created.session_token, "wrong")
    sessions.revoke(created.session_token)
    with pytest.raises(PermissionError, match="revoked"):
        sessions.validate(created.session_token, created.csrf_token)

    expiring = sessions.create()
    current[0] = now + timedelta(hours=8, seconds=1)
    with pytest.raises(PermissionError, match="expired"):
        sessions.validate(expiring.session_token, expiring.csrf_token)
