# BidMate RAG Evaluation Platform

BidMate is a portfolio snapshot of a team project for retrieval-augmented analysis of request-for-proposal documents. This public repository contains source code, tests, n8n workflow definitions, and a local evaluation-dataset review product. It contains no original procurement document, parsed confidential content, API key, runtime database, or inherited Git history.

## Product surfaces

- RAG ingestion, hybrid retrieval, grounded generation, and evaluation
- SOP-aligned n8n evaluation-dataset generation backed by a versioned Python worker
- FastAPI and Next.js `/eval-review` workflow for evidence review, approval, fork, and legacy CSV export

## Safe local demonstration

```powershell
uv sync --frozen --group dev
npm ci
npm --prefix web ci
uv run python scripts/prepare_public_demo.py --output artifacts/public_demo
uv run python scripts/run_public_rag_demo.py
powershell -ExecutionPolicy Bypass -File scripts/start_eval_tools.ps1 -Stack all -DataRoot artifacts/public_demo/source
```

All demonstration documents and databases are generated under ignored `artifacts/` paths. Configure a supported provider separately only when you intentionally want real-model execution.

## Validation

```powershell
uv run pytest -q
uv run ruff check src tests scripts
npm --prefix web run lint
npm --prefix web run build
uv run python -m scripts.publication.verify_public_snapshot --root . --scope all
```

No open-source license is asserted by this portfolio publication.