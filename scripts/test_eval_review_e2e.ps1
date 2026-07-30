$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$rebuildRoot = [IO.Path]::GetFullPath((Join-Path $root "artifacts\eval_dataset\rebuild"))
$runRoot = [IO.Path]::GetFullPath((Join-Path $rebuildRoot "verification\review-e2e"))
$approvedPrefix = $rebuildRoot.TrimEnd("\") + "\"
if (-not $runRoot.StartsWith($approvedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "refusing to use E2E runtime outside approved rebuild root: $runRoot"
}
if (Test-Path -LiteralPath $runRoot) {
    Remove-Item -LiteralPath $runRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $runRoot | Out-Null

$packageRoot = Join-Path $rebuildRoot "verification\n8n-mock\automation"
$package = Join-Path $packageRoot "candidate-package"
if (-not (Test-Path -LiteralPath (Join-Path $package "manifest.json"))) {
    throw "real 30-item n8n package is missing; run scripts\test_eval_automation_mock.ps1 first"
}
$database = Join-Path $runRoot "review.sqlite3"
$exportRoot = Join-Path $runRoot "exports"
$playwrightOutput = Join-Path $runRoot "playwright"
$backendPort = 18100 + ($PID % 500)
$webPort = 19100 + ($PID % 500)
$python = Join-Path $root ".venv\Scripts\python.exe"
$dataRoot = Join-Path $root "artifacts\public_demo\source"
$env:PYTHONPATH = Join-Path $root "src"
& $python (Join-Path $root "scripts\prepare_public_demo.py") --output (Join-Path $root "artifacts\public_demo") --documents 15
if ($LASTEXITCODE -ne 0) { throw "public synthetic demo preparation failed" }
$backend = $null
$env:PYTHONUNBUFFERED = "1"
$env:BIDMATE_EVAL_REVIEW_DB = $database
$env:BIDMATE_EVAL_REVIEW_PDF_ROOT = Join-Path $dataRoot "PDF1"
$env:BIDMATE_EVAL_PACKAGE_ROOT = $packageRoot
$env:BIDMATE_EVAL_EXPORT_ROOT = $exportRoot
$env:EVAL_REVIEW_API_ORIGIN = "http://127.0.0.1:$backendPort"
$env:EVAL_REVIEW_BACKEND_ORIGIN = $env:EVAL_REVIEW_API_ORIGIN
$env:EVAL_REVIEW_E2E_PORT = "$webPort"

try {
    $backend = Start-Process -FilePath $python -ArgumentList @(
        "-m", "uvicorn", "bidmate_rag.eval_dataset.review.api:app",
        "--host", "127.0.0.1", "--port", "$backendPort"
    ) -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runRoot "backend.stdout.log") -RedirectStandardError (Join-Path $runRoot "backend.stderr.log") -PassThru

    $deadline = (Get-Date).AddSeconds(45)
    do {
        if ($backend.HasExited) { throw "review backend exited early with code $($backend.ExitCode)" }
        try {
            $health = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$backendPort/api/packages" -TimeoutSec 2
        } catch {
            $health = $null
            Start-Sleep -Milliseconds 250
        }
    } until ($health -or (Get-Date) -ge $deadline)
    if (-not $health) { throw "review backend did not start" }

    Push-Location (Join-Path $root "web")
    try {
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "review E2E production build failed" }
        npm exec -- playwright test e2e/eval-review.spec.ts --workers=1 "--output=$playwrightOutput"
        if ($LASTEXITCODE -ne 0) { throw "review E2E Playwright test failed" }
    } finally {
        Pop-Location
    }

    $verify = @"
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from pathlib import Path
from sys import argv

from bidmate_rag.eval_dataset.contract.legacy_export import LEGACY_COLUMNS
from bidmate_rag.evaluation.dataset import load_eval_samples

database = Path(argv[1])
export_root = Path(argv[2])
evidence_path = Path(argv[3])
connection = sqlite3.connect(database)
connection.row_factory = sqlite3.Row
exported = connection.execute(
    "SELECT export_id, relative_path, checksum, item_count FROM review_exports ORDER BY created_at DESC LIMIT 1"
).fetchone()
assert exported is not None
bundle = export_root / exported["relative_path"]
standard_path = bundle / "legacy" / "standard" / "eval_batch_01.csv"
safety_path = bundle / "legacy" / "abstention_safety" / "eval_batch_01.csv"
with standard_path.open(encoding="utf-8-sig", newline="") as handle:
    standard = list(csv.DictReader(handle))
with safety_path.open(encoding="utf-8-sig", newline="") as handle:
    safety = list(csv.DictReader(handle))
assert standard and list(standard[0]) == LEGACY_COLUMNS
assert len(standard) == 27
assert len(safety) == 3
assert all(row["type"] == "D" for row in safety)
assert all(row["type"] != "D" for row in standard)
assert len(load_eval_samples(standard_path)) == 27
assert exported["item_count"] == 30
statuses = dict(connection.execute("SELECT status, COUNT(*) FROM review_items GROUP BY status").fetchall())
assert statuses == {"approved": 30, "rejected": 1}
summary = {
    "package_manifest_sha256": hashlib.sha256((Path(argv[4]) / "manifest.json").read_bytes()).hexdigest(),
    "export_id": exported["export_id"],
    "export_checksum": exported["checksum"],
    "standard_count": len(standard),
    "safety_count": len(safety),
    "approved_count": statuses["approved"],
    "rejected_count": statuses["rejected"],
    "standard_sha256": hashlib.sha256(standard_path.read_bytes()).hexdigest(),
    "safety_sha256": hashlib.sha256(safety_path.read_bytes()).hexdigest(),
    "browser_external_requests": 0,
}
evidence_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
"@
    $verify | & $python - $database $exportRoot (Join-Path $runRoot "summary.json") $package
    if ($LASTEXITCODE -ne 0) { throw "actual package export/Judge smoke verification failed" }
} finally {
    if ($backend -and -not $backend.HasExited) { Stop-Process -Id $backend.Id -Force }
    if ($backend) { [void]$backend.WaitForExit(5000) }
    $listener = Get-NetTCPConnection -State Listen -LocalPort $backendPort -ErrorAction SilentlyContinue
    if ($listener) { throw "review backend did not release loopback port $backendPort" }
}