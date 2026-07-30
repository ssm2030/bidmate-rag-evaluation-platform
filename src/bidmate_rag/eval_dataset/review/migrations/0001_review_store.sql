CREATE TABLE IF NOT EXISTS review_datasets (
    dataset_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    package_path TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS review_documents (
    document_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES review_datasets(dataset_id),
    document_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_items (
    item_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES review_datasets(dataset_id),
    revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    item_json TEXT NOT NULL,
    parent_snapshot_id TEXT
);

CREATE TABLE IF NOT EXISTS review_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL REFERENCES review_items(item_id),
    revision INTEGER NOT NULL,
    action TEXT NOT NULL,
    item_json TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS review_items_dataset_idx ON review_items(dataset_id);
CREATE INDEX IF NOT EXISTS review_snapshots_item_idx ON review_snapshots(item_id, snapshot_id DESC);

CREATE TRIGGER IF NOT EXISTS review_items_approved_immutable
BEFORE UPDATE ON review_items
WHEN OLD.status = 'approved'
BEGIN
  SELECT RAISE(ABORT, 'approved review items are immutable');
END;

CREATE TRIGGER IF NOT EXISTS review_items_rejected_immutable
BEFORE UPDATE ON review_items
WHEN OLD.status = 'rejected'
BEGIN
  SELECT RAISE(ABORT, 'rejected review items are immutable');
END;