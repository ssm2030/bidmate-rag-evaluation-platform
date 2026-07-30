## Summary

- Publish the clean-room BidMate RAG and evaluation platform with new Git history.
- Include deterministic, zero-cost synthetic demonstrations for RAG, n8n generation, and eval-review.
- Preserve Schema v2, evidence review, Judge-compatible CSV export, and SOP reference material.

## Validation

- [x] Python tests and Ruff
- [x] Deterministic RAG indexing, retrieval, and answer smoke test
- [x] n8n 30-candidate generation, retry, and idempotency test
- [x] Next.js lint/build and Playwright review/export E2E
- [x] Worktree and reachable Git-object publication scan
- [x] Codex Security review

## Publication boundaries

- Exactly 16 prompt assets are included.
- No original PDF, HWP/HWPX, Office document, archive, parsed source dataset, runtime database, credential, or previous Git object is included.
- The six files under `docs/reference/` are the only approved Markdown files copied from the private data area.
- Runtime fixtures are generated under ignored `artifacts/` and use loopback services only.
