# Live evaluation POC runbook

The local POC has an n8n generator, the Python automation worker, and the read-only evaluation review web app. Its default mode is mock and makes no provider request.

## Mock demo

Run `powershell -ExecutionPolicy Bypass -File scripts\start_eval_tools.ps1`. On a clean local n8n folder, the launcher imports all three workflows and publishes the Process workflow automatically. On an existing healthy folder, it refreshes only a generated runtime copy of the Generate workflow so its approved Batch Config values are literal while existing Process credentials remain intact. Global n8n environment-variable access stays blocked. Use `-RefreshWorkflows` only when deliberately replacing all workflow definitions, then recheck the three provider credential mappings in the n8n UI.

## Local stub path

Use an explicit loopback stub only for offline integration tests. The provider URL must be `http://127.0.0.1:<port>/v1`; do not use a LAN host or tunnel URL. The worker returns a prepared request before n8n sends any provider request, and it normalizes the provider response only after the corresponding ledger reservation exists.

## Public PDF preparation

Before a live run, review the public source URLs and then run:

```powershell
uv run python scripts\prepare_live_eval_poc.py --source-config configs\eval_live_sources.json --output-root artifacts\live_poc\source --target-count 12
```

Inspect `runtime-manifest.json`; it contains provenance, hashes, and file paths only—not extracted page text or contacts. Confirm every source has `public_provenance_checked: true` before paid execution.

## Paid calibration gate

Paid execution requires all of the following:

- current model and price verification in `configs/eval_live_models.json` (the placeholder blocks paid runs);
- `-Mode live -LiveAuthorized -CampaignKey <key>`;
- `-DataRoot artifacts\live_poc\source -TargetItems 5 -HardCapUsd 5.00`.

The `campaign_key` shares one USD 5 hard cap between calibration and any later full run. Inspect `GET /v1/runs/{run_id}/costs`; a reservation that cannot fit is rejected before a provider request. `unknown` calls remain reserved and must be reviewed rather than retried automatically.

## n8n credentials and review

Create the provider header/auth credential only in the n8n UI and assign it to the three `OpenAI <Stage>` HTTP Request nodes. Never place a key in workflow JSON, a command line, `.env`, or a log. After a calibration, inspect the review UI at `/eval-review` before deciding on the 30-item POC.

## Stop and recover

Stop local tools with `scripts\stop_eval_tools.ps1 -Stack all`. For a definite provider rejection or rate limit, record the provider failure endpoint so its reservation is released; ambiguous transport failures become `unknown` and remain excluded from automatic retry.
