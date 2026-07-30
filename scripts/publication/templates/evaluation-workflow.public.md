# Evaluation dataset workflow

The local evaluation workflow follows the bundled SOP while keeping generation and human review separate.

## 1. Prepare safe inputs

For a zero-cost demonstration, generate 15 fictional documents at runtime:

```powershell
uv run python scripts/prepare_public_demo.py --output artifacts/public_demo --documents 15
```

The command creates JSON source text, minimal PDFs, RAG Parquet files, and a batch manifest only under ignored `artifacts/public_demo/`.

## 2. Generate candidates in n8n

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_eval_tools.ps1 -Stack generator
```

Open `http://127.0.0.1:5678`, then execute **BidMate Eval Dataset - Generate v1**. The workflow inventories the selected batch, plans work units, generates SOP-aligned A-E question types through the local worker, resolves exact evidence, and writes a checksum-protected Schema v2 package. **BidMate Eval Dataset - Retry Failed v1** retries only eligible failed units. Re-running the generator is idempotent.

The deterministic verification command exercises 30 candidates, one transient retry, and a no-op replay:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/test_eval_automation_mock.ps1
```

## 3. Review evidence and approve

Start both surfaces or only the reviewer:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_eval_tools.ps1 -Stack all
```

Open `http://127.0.0.1:3000/eval-review`, import the discovered candidate package, and begin a local review session. Reviewers can edit structured fields, select exact PDF text, confirm normalized bounding boxes, save revisions, approve, reject, fork an approved item, and resume later.

## 4. Export

The export dialog writes an approved Schema v2 bundle plus legacy CSVs. Type D abstention/safety items are separated from standard answerable items, and both files remain compatible with the existing Judge loader.

## 5. Stop local services

```powershell
powershell -ExecutionPolicy Bypass -File scripts/stop_eval_tools.ps1 -Stack all
```

All service listeners bind to loopback. Runtime databases, packages, logs, and exports remain under ignored `artifacts/`.
