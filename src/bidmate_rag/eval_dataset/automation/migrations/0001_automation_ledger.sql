CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, cost_limit_microusd INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS work_units (work_unit_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, idempotency_key TEXT UNIQUE NOT NULL, status TEXT NOT NULL);
