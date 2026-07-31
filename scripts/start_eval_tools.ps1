param(
    [ValidateSet("generator", "reviewer", "all")]
    [string]$Stack = "all",
    [ValidateSet("mock", "live")]
    [string]$Mode = "mock",
    [string]$CampaignKey = "",
    [string]$DataRoot = "",
    [ValidateSet(5, 30)]
    [int]$TargetItems = 30,
    [decimal]$HardCapUsd = 0,
    [switch]$LiveAuthorized,
    [string]$StubProviderUrl = "",
    [switch]$RefreshWorkflows
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtimeRoot = Join-Path $projectRoot "artifacts\eval_dataset\rebuild\runtime"
$statePath = Join-Path $runtimeRoot "processes.json"
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$usingPublicDemo = $Mode -eq "mock" -and [string]::IsNullOrWhiteSpace($DataRoot)
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
if ($Mode -eq "live") {
    if (-not $LiveAuthorized) { throw "live mode requires -LiveAuthorized" }
    if ($HardCapUsd -ne [decimal]5.00) { throw "live mode requires -HardCapUsd 5.00" }
    if ([string]::IsNullOrWhiteSpace($CampaignKey)) { throw "live mode requires -CampaignKey" }
    if ([string]::IsNullOrWhiteSpace($DataRoot)) { throw "live mode requires -DataRoot" }
    $expectedLiveRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot "artifacts\live_poc\source"))
    if ($dataRoot -ne $expectedLiveRoot) { throw "live data root must be artifacts\live_poc\source" }
    foreach ($required in @("Batch_config.json", "Parsed", "PDF1", "runtime-manifest.json")) {
        if (-not (Test-Path -LiteralPath (Join-Path $dataRoot $required))) { throw "live data root is incomplete: $dataRoot (missing $required)" }
    }
    $liveManifest = Get-Content -Raw -LiteralPath (Join-Path $dataRoot "runtime-manifest.json") | ConvertFrom-Json
    if (@($liveManifest.sources).Count -lt 10 -or @($liveManifest.sources).Count -gt 12) { throw "live runtime manifest must contain 10 to 12 sources" }
    if (@($liveManifest.sources | Where-Object { -not $_.public_provenance_checked }).Count -gt 0) { throw "live runtime manifest has sources without public provenance confirmation" }
    $parsedRoot = [IO.Path]::GetFullPath((Join-Path $dataRoot 'Parsed'))
    $pdfRoot = [IO.Path]::GetFullPath((Join-Path $dataRoot 'PDF1'))
    $parsedPrefix = $parsedRoot.TrimEnd('\') + '\'
    $pdfPrefix = $pdfRoot.TrimEnd('\') + '\'
    $batchRows = Get-Content -Raw -LiteralPath (Join-Path $dataRoot 'Batch_config.json') | ConvertFrom-Json
    $batchRow = @($batchRows | Where-Object { [int]$_.batch_id -eq 1 })
    if ($batchRow.Count -ne 1) { throw 'live Batch_config must contain batch_id 1 exactly once' }
    $batchFiles = @($batchRow[0].files)
    $manifestFiles = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($source in @($liveManifest.sources)) {
        foreach ($field in @('parsed_file','pdf_file')) {
            $relative = [string]$source.$field
            if ([string]::IsNullOrWhiteSpace($relative) -or [IO.Path]::IsPathRooted($relative)) {
                throw "live runtime manifest $field must be a non-empty relative path"
            }
        }
        $parsedPath = [IO.Path]::GetFullPath((Join-Path $dataRoot ([string]$source.parsed_file)))
        $pdfPath = [IO.Path]::GetFullPath((Join-Path $dataRoot ([string]$source.pdf_file)))
        if (-not $parsedPath.StartsWith($parsedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "live runtime manifest parsed_file escapes Parsed: $parsedPath"
        }
        if (-not $pdfPath.StartsWith($pdfPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "live runtime manifest pdf_file escapes PDF1: $pdfPath"
        }
        if (-not (Test-Path -LiteralPath $parsedPath) -or -not (Test-Path -LiteralPath $pdfPath)) {
            throw 'live runtime manifest references a missing parsed or PDF file'
        }
        $actualPdfSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $pdfPath).Hash.ToLowerInvariant()
        if ($actualPdfSha256 -ne ([string]$source.pdf_sha256).ToLowerInvariant()) {
            throw "live runtime manifest PDF hash drift detected: $pdfPath"
        }
        $parsedPayload = [IO.File]::ReadAllText($parsedPath, [Text.UTF8Encoding]::new($false, $true)) | ConvertFrom-Json
        if (@($parsedPayload.pages).Count -ne [int]$source.page_count) {
            throw "live runtime manifest page count drift detected: $parsedPath"
        }
        if (-not $source.public_provenance_checked -or -not $source.empty_pages_within_threshold) {
            throw 'live runtime manifest provenance or page-quality gate failed'
        }
        if (-not $manifestFiles.Add([IO.Path]::GetFileName($parsedPath))) {
            throw 'live runtime manifest parsed_file values must be unique'
        }
    }
    if ($batchFiles.Count -ne $manifestFiles.Count -or @($batchFiles | Where-Object { -not $manifestFiles.Contains([string]$_) }).Count -gt 0) {
        throw 'live runtime manifest files do not match Batch_config'
    }
    $modelConfig = Get-Content -Raw -LiteralPath (Join-Path $projectRoot "configs\eval_live_models.json") | ConvertFrom-Json
    if ([string]::IsNullOrWhiteSpace($StubProviderUrl) -and $modelConfig.price_verified_at -eq "REVERIFY_BEFORE_PAID_RUN") { throw "live paid execution requires current model-price reverification" }
    if (-not [string]::IsNullOrWhiteSpace($StubProviderUrl)) {
        $stubUri = [Uri]$StubProviderUrl
        if ($stubUri.Scheme -ne "http" -or $stubUri.Host -ne "127.0.0.1") { throw "-StubProviderUrl must be an explicit http://127.0.0.1 URL" }
    }
}
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

function Test-N8nWorkflowSet {
    param([string]$DatabasePath, [string]$PythonPath)
    if (-not (Test-Path -LiteralPath $DatabasePath)) { return $false }
    $probeCode = @(
        'import sqlite3, sys',
        'required = {"bidmate-eval-generate-v1", "bidmate-eval-process-v1", "bidmate-eval-retry-v1"}',
        'connection = sqlite3.connect(sys.argv[1])',
        'placeholders = ",".join("?" for _ in required)',
        'rows = {row[0]: row[1:] for row in connection.execute(f"SELECT id, active, activeVersionId, versionId FROM workflow_entity WHERE id IN ({placeholders})", tuple(required))}',
        'process = rows.get("bidmate-eval-process-v1")',
        'valid = set(rows) == required and process[0] == 1 and process[1] == process[2]',
        'print("PASS" if valid else "FAIL")'
    ) -join "`n"
    $previousNativePreference = $PSNativeCommandUseErrorActionPreference
    try {
        $PSNativeCommandUseErrorActionPreference = $false
        $probeOutput = @($probeCode | & $PythonPath - $DatabasePath 2>$null)
        $probeExit = $LASTEXITCODE
    } finally {
        $PSNativeCommandUseErrorActionPreference = $previousNativePreference
    }
    return $probeExit -eq 0 -and (($probeOutput -join "`n").Trim() -eq "PASS")
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
        $env:BIDMATE_EVAL_MODE = $Mode
        $env:BIDMATE_EVAL_CAMPAIGN_KEY = $CampaignKey.Trim()
        $env:BIDMATE_EVAL_DATA_ROOT = $dataRoot
        $env:BIDMATE_EVAL_TARGET_COUNT = [string]$TargetItems
        $env:BIDMATE_EVAL_COST_LIMIT_MICROUSD = [string][int]($HardCapUsd * 1000000)
        $env:BIDMATE_EVAL_LIVE_AUTHORIZED = [string]$LiveAuthorized.IsPresent.ToString().ToLowerInvariant()
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

        if ($Mode -eq "live" -and -not [string]::IsNullOrWhiteSpace($StubProviderUrl)) {
            $env:BIDMATE_OPENAI_BASE_URL = $StubProviderUrl.TrimEnd("/")
            $env:BIDMATE_EVAL_STUB_MODE = "true"
        } else {
            Remove-Item Env:BIDMATE_OPENAI_BASE_URL -ErrorAction SilentlyContinue
            Remove-Item Env:BIDMATE_EVAL_STUB_MODE -ErrorAction SilentlyContinue
        }
        $runtimeWorkflowRoot = Join-Path $runtimeRoot "workflows"
        if (Test-Path -LiteralPath $runtimeWorkflowRoot) {
            Remove-Item -LiteralPath $runtimeWorkflowRoot -Recurse -Force
        }
        New-Item -ItemType Directory -Force -Path $runtimeWorkflowRoot | Out-Null
        foreach ($workflowName in @("bidmate_eval_generate_v1.json", "bidmate_eval_process_work_unit_v1.json", "bidmate_eval_retry_failed_v1.json")) {
            Copy-Item -LiteralPath (Join-Path $workflowRoot $workflowName) -Destination (Join-Path $runtimeWorkflowRoot $workflowName)
        }
        $runtimeGeneratePath = Join-Path $runtimeWorkflowRoot "bidmate_eval_generate_v1.json"
        $runtimeGenerate = Get-Content -Raw -LiteralPath $runtimeGeneratePath | ConvertFrom-Json
        $runtimeBatchConfig = @($runtimeGenerate.nodes | Where-Object { $_.name -eq "Batch Config" })[0]
        if ($null -eq $runtimeBatchConfig) { throw "runtime Generate workflow is missing Batch Config" }
        $runtimeBatchValues = @{
            batch_id = 1
            target_count = $TargetItems
            mode = $Mode
            max_items_per_call = 5
            campaign_key = if ([string]::IsNullOrWhiteSpace($CampaignKey)) { $null } else { $CampaignKey.Trim() }
            data_root = $dataRoot
            cost_limit_microusd = [int]($HardCapUsd * 1000000)
            live_authorized = [bool]$LiveAuthorized.IsPresent
        }
        foreach ($assignment in @($runtimeBatchConfig.parameters.assignments.assignments)) {
            if ($runtimeBatchValues.ContainsKey([string]$assignment.name)) {
                $assignment.value = $runtimeBatchValues[[string]$assignment.name]
            }
        }
        [IO.File]::WriteAllText($runtimeGeneratePath, ($runtimeGenerate | ConvertTo-Json -Depth 100), [Text.UTF8Encoding]::new($false))

        $workflowDatabase = Join-Path $env:N8N_USER_FOLDER ".n8n\database.sqlite"
        $needsWorkflowRefresh = $RefreshWorkflows.IsPresent -or -not (Test-N8nWorkflowSet -DatabasePath $workflowDatabase -PythonPath $python)
        if ($needsWorkflowRefresh) {
            & $n8nCli import:workflow --separate "--input=$runtimeWorkflowRoot" | Out-File -Encoding utf8 (Join-Path $runtimeRoot "n8n-import.log")
            if ($LASTEXITCODE -ne 0) { throw "n8n workflow import failed" }
            & $n8nCli publish:workflow --id=bidmate-eval-process-v1 | Out-File -Encoding utf8 (Join-Path $runtimeRoot "n8n-publish.log")
            if ($LASTEXITCODE -ne 0) { throw "n8n process workflow publish failed" }
        } else {
            & $n8nCli import:workflow "--input=$runtimeGeneratePath" | Out-File -Encoding utf8 (Join-Path $runtimeRoot "n8n-generate-import.log")
            if ($LASTEXITCODE -ne 0) { throw "n8n Generate workflow runtime refresh failed" }
        }
        if (-not (Test-N8nWorkflowSet -DatabasePath $workflowDatabase -PythonPath $python)) {
            throw "n8n workflow bootstrap verification failed"
        }

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