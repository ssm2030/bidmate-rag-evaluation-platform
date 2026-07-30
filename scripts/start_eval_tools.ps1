param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("generator", "reviewer", "all")]
    [string]$Stack,
    [string]$DataRoot = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtimeRoot = Join-Path $projectRoot "artifacts\eval_dataset\rebuild\runtime"
$statePath = Join-Path $runtimeRoot "processes.json"
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$usingPublicDemo = [string]::IsNullOrWhiteSpace($DataRoot)
if ($usingPublicDemo) {
    $dataRoot = Join-Path $projectRoot "artifacts\public_demo\source"
} elseif ([IO.Path]::IsPathRooted($DataRoot)) {
    $dataRoot = [IO.Path]::GetFullPath($DataRoot)
} else {
    $dataRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot $DataRoot))
}
$workflowRoot = Join-Path $projectRoot "n8n\workflows"
$startedHandles = [Collections.Generic.List[object]]::new()
$records = [Collections.Generic.List[object]]::new()

if (-not (Test-Path -LiteralPath $python)) {
    throw "project Python is missing: $python"
}
$env:PYTHONPATH = Join-Path $projectRoot "src"
if ($usingPublicDemo -and -not (Test-Path -LiteralPath (Join-Path $dataRoot "Batch_config.json"))) {
    & $python (Join-Path $projectRoot "scripts\prepare_public_demo.py") --output (Join-Path $projectRoot "artifacts\public_demo") --documents 15
    if ($LASTEXITCODE -ne 0) { throw "public synthetic demo preparation failed" }
}
if ($Stack -in @("generator", "all")) {
    foreach ($required in @("Batch_config.json", "Parsed", "PDF1")) {
        if (-not (Test-Path -LiteralPath (Join-Path $dataRoot $required))) {
            throw "generator data root is incomplete: $dataRoot (missing $required)"
        }
    }
} elseif (-not (Test-Path -LiteralPath (Join-Path $dataRoot "PDF1"))) {
    throw "review PDF root is missing under $dataRoot"
}
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null

if (Test-Path -LiteralPath $statePath) {
    $existingState = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
    $live = @($existingState.processes | Where-Object { Get-Process -Id $_.pid -ErrorAction SilentlyContinue })
    if ($live.Count -gt 0) {
        throw "BidMate eval tools are already running. Use scripts\stop_eval_tools.ps1 first."
    }
    [IO.File]::Delete($statePath)
}

function Assert-PortFree {
    param([int]$Port)
    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if ($listener) { throw "127.0.0.1:$Port is already in use" }
}

function Wait-Health {
    param([string]$Uri, [string]$Label, [int]$Port, [int]$Seconds = 60)
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) { break }
        } catch {
            Start-Sleep -Milliseconds 300
        }
    } while ((Get-Date) -lt $deadline)
    if ((Get-Date) -ge $deadline) { throw "$Label did not become healthy at $Uri" }
    $owner = Get-NetTCPConnection -State Listen -LocalAddress "127.0.0.1" -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty OwningProcess
    if (-not $owner) { throw "$Label health responded but no loopback listener owner was found" }
    return [int]$owner
}

function Add-Record {
    param([string]$Name, [string]$StackName, [int]$Port, [int]$ProcessId, [string]$CommandToken)
    $records.Add([pscustomobject]@{
        name = $Name
        stack = $StackName
        pid = $ProcessId
        port = $Port
        command_token = $CommandToken
    })
}

try {
    if ($Stack -in @("generator", "all")) {
        Assert-PortFree 8121
        Assert-PortFree 5678
        $n8nCommand = Get-Command "n8n.cmd" -ErrorAction SilentlyContinue
        if ($null -eq $n8nCommand) { throw "n8n CLI is not installed" }
        $n8nCli = $n8nCommand.Source
        $n8nVersion = (& $n8nCli --version).Trim()
        if ($n8nVersion -ne "2.15.1") { throw "n8n 2.15.1 is required; found $n8nVersion" }

        $env:PYTHONPATH = Join-Path $projectRoot "src"
        $env:PYTHONUNBUFFERED = "1"
        $env:BIDMATE_EVAL_MODE = "mock"
        $env:BIDMATE_EVAL_SIMULATE_TRANSIENT_FAILURE = "false"
        $env:BIDMATE_EVAL_AUTOMATION_LEDGER = Join-Path $projectRoot "artifacts\eval_dataset\rebuild\automation\ledger.sqlite3"
        $env:BIDMATE_EVAL_AUTOMATION_PACKAGE = Join-Path $projectRoot "artifacts\eval_dataset\rebuild\automation\candidate-package"
        $env:BIDMATE_EVAL_BATCH_CONFIG = Join-Path $dataRoot "Batch_config.json"
        $env:BIDMATE_EVAL_JSON_ROOT = Join-Path $dataRoot "Parsed"
        $env:BIDMATE_EVAL_PDF_ROOT = Join-Path $dataRoot "PDF1"
        $env:BIDMATE_EVAL_INVENTORY_CACHE = Join-Path $projectRoot "artifacts\eval_dataset\rebuild\automation\cache"
        $env:N8N_USER_FOLDER = Join-Path $runtimeRoot "n8n-user"
        $env:N8N_HOST = "127.0.0.1"
        $env:N8N_LISTEN_ADDRESS = "127.0.0.1"
        $env:N8N_PORT = "5678"
        $env:N8N_PROTOCOL = "http"
        $env:N8N_SECURE_COOKIE = "false"
        $env:N8N_DIAGNOSTICS_ENABLED = "false"
        $env:N8N_VERSION_NOTIFICATIONS_ENABLED = "false"
        $env:N8N_TEMPLATES_ENABLED = "false"
        New-Item -ItemType Directory -Force -Path $env:N8N_USER_FOLDER | Out-Null

        & $n8nCli import:workflow --separate "--input=$workflowRoot" | Out-File -Encoding utf8 (Join-Path $runtimeRoot "n8n-import.log")
        if ($LASTEXITCODE -ne 0) { throw "n8n workflow import failed" }
        & $n8nCli publish:workflow --id=bidmate-eval-process-v1 | Out-File -Encoding utf8 (Join-Path $runtimeRoot "n8n-publish.log")
        if ($LASTEXITCODE -ne 0) { throw "n8n process workflow publish failed" }

        $worker = Start-Process -FilePath $python -ArgumentList @(
            "-m", "uvicorn", "bidmate_rag.eval_dataset.automation.api:app",
            "--host", "127.0.0.1", "--port", "8121"
        ) -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeRoot "worker.stdout.log") -RedirectStandardError (Join-Path $runtimeRoot "worker.stderr.log") -PassThru
        $startedHandles.Add($worker)
        $workerPid = Wait-Health -Uri "http://127.0.0.1:8121/v1/health" -Label "automation worker" -Port 8121
        Add-Record -Name "automation-worker" -StackName "generator" -Port 8121 -ProcessId $workerPid -CommandToken "bidmate_rag.eval_dataset.automation.api:app"

        $n8n = Start-Process -FilePath $n8nCli -ArgumentList @("start") -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeRoot "n8n.stdout.log") -RedirectStandardError (Join-Path $runtimeRoot "n8n.stderr.log") -PassThru
        $startedHandles.Add($n8n)
        $n8nPid = Wait-Health -Uri "http://127.0.0.1:5678/healthz" -Label "n8n" -Port 5678 -Seconds 90
        Add-Record -Name "n8n" -StackName "generator" -Port 5678 -ProcessId $n8nPid -CommandToken "n8n"
    }

    if ($Stack -in @("reviewer", "all")) {
        Assert-PortFree 8101
        Assert-PortFree 3000
        if (-not (Test-Path -LiteralPath (Join-Path $projectRoot "web\.next\BUILD_ID"))) {
            throw "review web production build is missing; run npm --prefix web run build"
        }
        $npmCommand = Get-Command "npm.cmd" -ErrorAction Stop
        $env:PYTHONPATH = Join-Path $projectRoot "src"
        $env:BIDMATE_EVAL_REVIEW_DB = Join-Path $projectRoot "artifacts\eval_dataset\rebuild\review\review.sqlite3"
        $env:BIDMATE_EVAL_REVIEW_PDF_ROOT = Join-Path $dataRoot "PDF1"
        $env:BIDMATE_EVAL_PACKAGE_ROOT = Join-Path $projectRoot "artifacts\eval_dataset\rebuild\automation"
        $env:BIDMATE_EVAL_EXPORT_ROOT = Join-Path $projectRoot "artifacts\eval_dataset\rebuild\review\exports"
        $env:EVAL_REVIEW_API_ORIGIN = "http://127.0.0.1:8101"

        $reviewApi = Start-Process -FilePath $python -ArgumentList @(
            "-m", "uvicorn", "bidmate_rag.eval_dataset.review.api:app",
            "--host", "127.0.0.1", "--port", "8101"
        ) -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeRoot "review-api.stdout.log") -RedirectStandardError (Join-Path $runtimeRoot "review-api.stderr.log") -PassThru
        $startedHandles.Add($reviewApi)
        $reviewApiPid = Wait-Health -Uri "http://127.0.0.1:8101/api/packages" -Label "review API" -Port 8101
        Add-Record -Name "review-api" -StackName "reviewer" -Port 8101 -ProcessId $reviewApiPid -CommandToken "bidmate_rag.eval_dataset.review.api:app"

        $reviewWeb = Start-Process -FilePath $npmCommand.Source -ArgumentList @(
            "--prefix", "web", "run", "start", "--", "--hostname", "127.0.0.1", "--port", "3000"
        ) -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeRoot "review-web.stdout.log") -RedirectStandardError (Join-Path $runtimeRoot "review-web.stderr.log") -PassThru
        $startedHandles.Add($reviewWeb)
        $reviewWebPid = Wait-Health -Uri "http://127.0.0.1:3000/eval-review" -Label "review web" -Port 3000 -Seconds 90
        Add-Record -Name "review-web" -StackName "reviewer" -Port 3000 -ProcessId $reviewWebPid -CommandToken "next"
    }

    $state = [ordered]@{
        started_at = (Get-Date).ToString("o")
        stack = $Stack
        processes = @($records)
    }
    [IO.File]::WriteAllText($statePath, ($state | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
} catch {
    for ($index = $startedHandles.Count - 1; $index -ge 0; $index--) {
        $handle = $startedHandles[$index]
        if ($handle -and -not $handle.HasExited) { Stop-Process -Id $handle.Id -Force -ErrorAction SilentlyContinue }
    }
    throw
}

if ($Stack -in @("generator", "all")) {
    Write-Host "n8n generator: http://127.0.0.1:5678"
    Write-Host "  1) Open 'BidMate Eval Dataset - Generate v1' and click Execute Workflow."
    Write-Host "  2) If a failed work unit exists, open 'BidMate Eval Dataset - Retry Failed v1' and click Execute Workflow."
}
if ($Stack -in @("reviewer", "all")) {
    Write-Host "review web:     http://127.0.0.1:3000/eval-review"
    Write-Host "  After generation completes, refresh this page."
    Write-Host "  Choose the discovered package, click Import, then click Begin review."
}
Write-Host "stop: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop_eval_tools.ps1 -Stack $Stack"