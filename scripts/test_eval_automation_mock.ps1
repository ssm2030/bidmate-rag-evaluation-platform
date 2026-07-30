param([int]$WorkerPort = 8121)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$rebuildRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot 'artifacts\eval_dataset\rebuild'))
$runRoot = [IO.Path]::GetFullPath((Join-Path $rebuildRoot 'verification\n8n-mock'))
$runtimePrefix = $rebuildRoot.TrimEnd('\') + '\'
if (-not $runRoot.StartsWith($runtimePrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "refusing to use runtime outside approved rebuild root: $runRoot"
}
if (Test-Path -LiteralPath $runRoot) {
    Remove-Item -LiteralPath $runRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $runRoot | Out-Null

$workflowRoot = Join-Path $projectRoot 'n8n\workflows'
$n8nCommand = Get-Command 'n8n.cmd' -ErrorAction SilentlyContinue
if ($null -eq $n8nCommand) {
    throw 'n8n CLI is not installed'
}
$n8nCli = $n8nCommand.Source
$n8nVersion = (& $n8nCli --version).Trim()
if ($LASTEXITCODE -ne 0 -or $n8nVersion -ne '2.15.1') {
    throw "n8n 2.15.1 is required; found '$n8nVersion' at $n8nCli"
}
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw "project Python is not available at $python"
}

$dataRoot = Join-Path $projectRoot 'artifacts\public_demo\source'
$env:PYTHONPATH = Join-Path $projectRoot 'src'
& $python (Join-Path $projectRoot 'scripts\prepare_public_demo.py') --output (Join-Path $projectRoot 'artifacts\public_demo') --documents 15
if ($LASTEXITCODE -ne 0) { throw 'public synthetic demo preparation failed' }
$env:PYTHONUNBUFFERED = '1'
$env:BIDMATE_EVAL_MODE = 'mock'
$env:BIDMATE_EVAL_SIMULATE_TRANSIENT_FAILURE = 'true'
$env:BIDMATE_EVAL_AUTOMATION_LEDGER = Join-Path $runRoot 'automation\ledger.sqlite3'
$env:BIDMATE_EVAL_AUTOMATION_PACKAGE = Join-Path $runRoot 'automation\candidate-package'
$env:BIDMATE_EVAL_BATCH_CONFIG = Join-Path $dataRoot 'Batch_config.json'
$env:BIDMATE_EVAL_JSON_ROOT = Join-Path $dataRoot 'Parsed'
$env:BIDMATE_EVAL_PDF_ROOT = Join-Path $dataRoot 'PDF1'
$env:BIDMATE_EVAL_INVENTORY_CACHE = Join-Path $rebuildRoot 'automation\cache'
$env:N8N_USER_FOLDER = Join-Path $runRoot 'n8n-user'
$env:N8N_ENCRYPTION_KEY = [guid]::NewGuid().ToString('N')
$env:N8N_DIAGNOSTICS_ENABLED = 'false'
$env:N8N_VERSION_NOTIFICATIONS_ENABLED = 'false'
$env:N8N_TEMPLATES_ENABLED = 'false'
$env:N8N_LOG_LEVEL = 'error'
New-Item -ItemType Directory -Force -Path $env:N8N_USER_FOLDER | Out-Null

$workerOut = Join-Path $runRoot 'worker.stdout.log'
$workerErr = Join-Path $runRoot 'worker.stderr.log'
$worker = $null

function Invoke-N8n {
    param([string[]]$Arguments, [string]$LogPath)
    $output = @(& $n8nCli @Arguments 2>&1)
    [IO.File]::WriteAllLines($LogPath, [string[]]$output, [Text.UTF8Encoding]::new($false))
    if ($LASTEXITCODE -ne 0) {
        $tail = $output | Select-Object -Last 20 | Out-String
        throw "n8n command failed ($($Arguments -join ' ')):`n$tail"
    }
}

function Get-LedgerState {
    $code = 'import json, os; from bidmate_rag.eval_dataset.automation.ledger import AutomationLedger; ledger=AutomationLedger(os.environ["BIDMATE_EVAL_AUTOMATION_LEDGER"]); run_id=ledger.run_id_for_dataset("batch-1"); assert run_id is not None; print(json.dumps({"run_id": run_id, **ledger.summary(run_id)}, sort_keys=True))'
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($code))
    $wrapper = "import base64;exec(base64.b64decode('$encoded'))"
    $raw = & $python -c $wrapper
    if ($LASTEXITCODE -ne 0 -or -not $raw) { throw 'ledger state verification failed' }
    return $raw | ConvertFrom-Json
}

function Get-ProviderCallCount {
    if (-not (Test-Path -LiteralPath $workerOut)) { return 0 }
    $selector = @(Select-String -LiteralPath $workerOut -SimpleMatch 'POST /v1/workflow/mock-selector').Count
    $generator = @(Select-String -LiteralPath $workerOut -SimpleMatch 'POST /v1/workflow/mock-generator').Count
    return $selector + $generator
}

try {
    $worker = Start-Process -FilePath $python -ArgumentList @(
        '-m', 'uvicorn', 'bidmate_rag.eval_dataset.automation.api:app',
        '--host', '127.0.0.1', '--port', "$WorkerPort"
    ) -WindowStyle Hidden -RedirectStandardOutput $workerOut -RedirectStandardError $workerErr -PassThru

    $deadline = (Get-Date).AddSeconds(45)
    do {
        if ($worker.HasExited) { throw "automation worker exited early with code $($worker.ExitCode)" }
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$WorkerPort/v1/health" -TimeoutSec 2
        } catch {
            $health = $null
            Start-Sleep -Milliseconds 250
        }
    } until ($health -or (Get-Date) -ge $deadline)
    if (-not $health -or $health.status -ne 'ready' -or $health.mode -ne 'mock') {
        throw 'local automation worker did not become mock-ready'
    }

    Invoke-N8n -Arguments @('import:workflow', '--separate', "--input=$workflowRoot") -LogPath (Join-Path $runRoot 'import.log')
    Invoke-N8n -Arguments @('publish:workflow', '--id=bidmate-eval-process-v1') -LogPath (Join-Path $runRoot 'publish-process.log')

    Invoke-N8n -Arguments @('execute', '--id=bidmate-eval-generate-v1', '--rawOutput') -LogPath (Join-Path $runRoot 'main-first.log')
    $afterFirst = Get-LedgerState
    if ($afterFirst.candidate_count -ne 30 -or $afterFirst.done_count -ne 29 -or $afterFirst.retryable_count -ne 1) {
        throw "first n8n pass did not preserve exactly one retryable unit: $($afterFirst | ConvertTo-Json -Compress)"
    }

    $retryWorkflow = Get-Content -Raw -LiteralPath (Join-Path $workflowRoot 'bidmate_eval_retry_failed_v1.json') | ConvertFrom-Json
    $retryAssignment = $retryWorkflow.nodes |
        Where-Object name -eq 'Retry Settings' |
        Select-Object -ExpandProperty parameters |
        Select-Object -ExpandProperty assignments |
        Select-Object -ExpandProperty assignments |
        Where-Object name -eq 'run_id'
    if ($null -eq $retryAssignment) { throw 'retry workflow run_id assignment is missing' }
    $retryAssignment.value = $afterFirst.run_id
    $runtimeRetry = Join-Path $runRoot 'bidmate_eval_retry_failed_runtime.json'
    [IO.File]::WriteAllText(
        $runtimeRetry,
        ($retryWorkflow | ConvertTo-Json -Depth 100),
        [Text.UTF8Encoding]::new($false)
    )
    Invoke-N8n -Arguments @('import:workflow', "--input=$runtimeRetry") -LogPath (Join-Path $runRoot 'import-retry.log')
    Invoke-N8n -Arguments @('execute', '--id=bidmate-eval-retry-v1', '--rawOutput') -LogPath (Join-Path $runRoot 'retry.log')

    $afterRetry = Get-LedgerState
    if ($afterRetry.done_count -ne 30 -or $afterRetry.retryable_count -ne 0 -or $afterRetry.retry_count -ne 1) {
        throw "retry workflow did not complete the one eligible unit: $($afterRetry | ConvertTo-Json -Compress)"
    }

    Invoke-N8n -Arguments @('execute', '--id=bidmate-eval-generate-v1', '--rawOutput') -LogPath (Join-Path $runRoot 'main-finalize.log')
    $packageCode = 'import hashlib, json, os, sqlite3; from collections import Counter; from pathlib import Path; from bidmate_rag.eval_dataset.contract.package_io import read_package; root=Path(os.environ["BIDMATE_EVAL_AUTOMATION_PACKAGE"]); package=read_package(root); items=package["items"]; conn=sqlite3.connect(os.environ["BIDMATE_EVAL_AUTOMATION_LEDGER"]); attempts=conn.execute("select sum(attempts), max(attempts) from work_units").fetchone(); result={"documents":len(package["documents"]),"items":len(items),"anchors":sum(len(i["evidence_anchors"]) for i in items),"types":dict(Counter(i["provenance"]["sop_type"] for i in items)),"difficulties":dict(Counter(i["difficulty"] for i in items)),"attempts":attempts[0],"max_attempts":attempts[1],"manifest_sha256":hashlib.sha256((root/"manifest.json").read_bytes()).hexdigest()}; print(json.dumps(result, sort_keys=True))'
    $packageEncoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($packageCode))
    $packageWrapper = "import base64;exec(base64.b64decode('$packageEncoded'))"
    $packageRaw = & $python -c $packageWrapper
    if ($LASTEXITCODE -ne 0 -or -not $packageRaw) { throw 'candidate package verification failed' }
    $packageEvidence = $packageRaw | ConvertFrom-Json
    if ($packageEvidence.documents -ne 15 -or $packageEvidence.items -ne 30 -or $packageEvidence.anchors -ne 39) {
        throw "candidate package cardinality is invalid: $packageRaw"
    }
    if (($packageEvidence.types | ConvertTo-Json -Compress) -ne '{"A":9,"B":12,"C":3,"D":3,"E":3}') {
        throw "SOP type distribution is invalid: $packageRaw"
    }
    if (($packageEvidence.difficulties | ConvertTo-Json -Compress) -ne '{"high":6,"low":15,"medium":9}') {
        throw "difficulty distribution is invalid: $packageRaw"
    }
    if ($packageEvidence.attempts -ne 31 -or $packageEvidence.max_attempts -ne 2) {
        throw "retry attempt ledger is invalid: $packageRaw"
    }

    $providerCallsBefore = Get-ProviderCallCount
    $manifestBefore = $packageEvidence.manifest_sha256
    Invoke-N8n -Arguments @('execute', '--id=bidmate-eval-generate-v1', '--rawOutput') -LogPath (Join-Path $runRoot 'main-idempotent.log')
    $providerCallsAfter = Get-ProviderCallCount
    $manifestAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $env:BIDMATE_EVAL_AUTOMATION_PACKAGE 'manifest.json')).Hash.ToLowerInvariant()
    $finalState = Get-LedgerState
    $duplicateProviderCalls = $providerCallsAfter - $providerCallsBefore
    $manifestHashStable = $manifestAfter -eq $manifestBefore
    if ($duplicateProviderCalls -ne 0 -or -not $manifestHashStable) {
        throw "idempotent replay changed provider calls or package hash: calls=$duplicateProviderCalls stable=$manifestHashStable"
    }
    if ($finalState.status -ne 'completed' -or $finalState.done_count -ne 30 -or $finalState.retry_count -ne 1) {
        throw "final ledger state is invalid: $($finalState | ConvertTo-Json -Compress)"
    }

    $evidence = [ordered]@{
        status = 'PASS'
        run_id = $finalState.run_id
        package_path = $env:BIDMATE_EVAL_AUTOMATION_PACKAGE
        documents = $packageEvidence.documents
        items = $packageEvidence.items
        anchors = $packageEvidence.anchors
        types = $packageEvidence.types
        difficulties = $packageEvidence.difficulties
        retry_count = $finalState.retry_count
        duplicateProviderCalls = $duplicateProviderCalls
        manifestHashStable = $manifestHashStable
        manifest_sha256 = $manifestAfter
    }
    $evidenceJson = $evidence | ConvertTo-Json -Depth 10
    [IO.File]::WriteAllText((Join-Path $runRoot 'generator-evidence.json'), $evidenceJson, [Text.UTF8Encoding]::new($false))
    Write-Output $evidenceJson
}
finally {
    if ($null -ne $worker -and -not $worker.HasExited) {
        Stop-Process -Id $worker.Id -Force
        $worker.WaitForExit(5000) | Out-Null
    }
}