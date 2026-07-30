CREATE TABLE IF NOT EXISTS review_sessions (
    session_id TEXT PRIMARY KEY,
    session_hash TEXT NOT NULL UNIQUE,
    csrf_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1))
);

CREATE TABLE IF NOT EXISTS review_resume (
    dataset_id TEXT PRIMARY KEY REFERENCES review_datasets(dataset_id),
    item_id TEXT NOT NULL REFERENCES review_items(item_id),
    anchor_id TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS review_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id TEXT NOT NULL REFERENCES review_datasets(dataset_id),
    item_id TEXT,
    revision INTEGER,
    event_type TEXT NOT NULL,
    actor_session_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS review_exports (
    export_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES review_datasets(dataset_id),
    kind TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    checksum TEXT NOT NULL,
    item_count INTEGER NOT NULL,
    actor_session_id TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS review_events_dataset_idx
ON review_events(dataset_id, event_id DESC);
CREATE INDEX IF NOT EXISTS review_events_item_idx
ON review_events(item_id, event_id DESC);
CREATE INDEX IF NOT EXISTS review_exports_dataset_idx
ON review_exports(dataset_id, created_at DESC);

CREATE TRIGGER IF NOT EXISTS review_events_no_update
BEFORE UPDATE ON review_events
BEGIN
  SELECT RAISE(ABORT, 'review events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS review_events_no_delete
BEFORE DELETE ON review_events
BEGIN
  SELECT RAISE(ABORT, 'review events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS review_snapshots_no_update
BEFORE UPDATE ON review_snapshots
BEGIN
  SELECT RAISE(ABORT, 'review snapshots are append-only');
END;

CREATE TRIGGER IF NOT EXISTS review_snapshots_no_delete
BEFORE DELETE ON review_snapshots
BEGIN
  SELECT RAISE(ABORT, 'review snapshots are append-only');
END;