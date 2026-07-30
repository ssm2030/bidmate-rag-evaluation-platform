# Using local documents safely

This repository intentionally ships without procurement documents, parsed source text, or runtime databases. The supported pattern is to bring your own documents locally and keep every derived artifact outside Git.

## Local-only layout

The public `.gitignore` blocks `data/`, `artifacts/`, document binaries, Parquet files, SQLite databases, logs, and model weights. A conventional local layout is:

```text
data/
  raw/
    metadata/data_list.csv
    rfp/
  processed/
artifacts/
```

Before processing real files, confirm that `git status --short` does not list them. Do not override the ignore rules to publish source documents.

## Ingestion and indexing

Run ingestion with explicit local paths:

```powershell
uv run python scripts/ingest_data.py --metadata-path data/raw/metadata/data_list.csv --raw-dir data/raw/rfp --output-dir data/processed
```

For a no-network index, use the deterministic embedding provider:

```powershell
uv run python scripts/build_index.py --provider-config configs/providers/deterministic_demo.yaml --chunks-path data/processed/chunks.parquet --persist-dir artifacts/chroma_db
```

Real providers may require credentials and may transmit document text to an external service. Configure them only after reviewing data classification, provider terms, cost limits, and organizational approval. Never commit `.env` or print credentials.

## Evaluation tools with user-supplied data

The generator accepts a local data root containing `Batch_config.json`, `Parsed/`, and `PDF1/`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_eval_tools.ps1 -Stack all -DataRoot data/evaluation
```

The batch JSON and PDF names must map one-to-one. Keep generated candidate packages, the automation ledger, review database, and exports under `artifacts/`.

## Pre-publication check

Run the publication guard before any commit or push:

```powershell
uv run python -m scripts.publication.verify_public_snapshot --root . --scope all
```

A pass confirms the repository contains exactly 16 approved prompt assets and no blocked document extension, document signature, credential pattern, personal path, oversized blob, or unsafe n8n remote.
