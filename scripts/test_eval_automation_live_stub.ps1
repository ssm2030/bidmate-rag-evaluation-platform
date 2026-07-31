param(
    [string]$RunRoot = (Join-Path 'C:\tmp' ("bidmate-live-stub-" + [guid]::NewGuid().ToString('N'))),
    [int]$WorkerPort = 18121,
    [int]$StubPort = 18900,
    [int]$RunnerBrokerPort = 15679
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$runRoot = [IO.Path]::GetFullPath($RunRoot)
$tempRoot = [IO.Path]::GetFullPath('C:\tmp').TrimEnd('\') + '\'
if (-not $runRoot.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "RunRoot must be inside C:\tmp: $runRoot"
}
if (Test-Path -LiteralPath $runRoot) {
    Remove-Item -LiteralPath $runRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $runRoot | Out-Null

$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$n8nCommand = Get-Command 'n8n.cmd' -ErrorAction SilentlyContinue
if (-not (Test-Path -LiteralPath $python) -or $null -eq $n8nCommand) {
    throw 'project Python and n8n.cmd are required for the live stub E2E'
}
$n8nCli = $n8nCommand.Source
$n8nVersion = (& $n8nCli --version).Trim()
if ($LASTEXITCODE -ne 0 -or $n8nVersion -ne '2.15.1') {
    throw "n8n 2.15.1 is required; found '$n8nVersion'"
}

function Assert-PortFree([int]$Port) {
    if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
        throw "required loopback port is already listening: $Port"
    }
}
function Wait-Ready([string]$Uri, [string]$Name, $Process) {
    $deadline = (Get-Date).AddSeconds(45)
    do {
        if ($Process.HasExited) { throw "$Name exited early with code $($Process.ExitCode)" }
        try { return Invoke-RestMethod -Uri $Uri -TimeoutSec 2 } catch { Start-Sleep -Milliseconds 250 }
    } until ((Get-Date) -ge $deadline)
    throw "$Name did not become ready"
}
function Invoke-N8n([string[]]$Arguments, [string]$LogPath) {
    $previous = $PSNativeCommandUseErrorActionPreference
    try {
        $PSNativeCommandUseErrorActionPreference = $false
        $output = @(& $n8nCli @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $PSNativeCommandUseErrorActionPreference = $previous
    }
    [IO.File]::WriteAllLines($LogPath, [string[]]$output, [Text.UTF8Encoding]::new($false))
    if ($exitCode -ne 0) { throw "n8n command failed with exit ${exitCode}: $($Arguments -join ' ')" }
}
function Invoke-JsonPost([string]$Uri, [object]$Payload) {
    return Invoke-RestMethod -Uri $Uri -Method Post -ContentType 'application/json' -Body ($Payload | ConvertTo-Json -Depth 100 -Compress) -TimeoutSec 15
}
function Set-StubScenarioPlan([string[]]$Scenarios) {
    return Invoke-RestMethod -Uri "http://127.0.0.1:$StubPort/scenario-plan" -Method Post -ContentType 'application/json' -Body (@{ scenarios = @($Scenarios) } | ConvertTo-Json -Compress) -TimeoutSec 15
}
function Get-LedgerState {
    $code = 'import json,os; from bidmate_rag.eval_dataset.automation.ledger import AutomationLedger; ledger=AutomationLedger(os.environ["BIDMATE_EVAL_AUTOMATION_LEDGER"]); run_id=ledger.run_id_for_dataset("batch-1"); assert run_id; print(json.dumps({"run_id":run_id,**ledger.summary(run_id)},sort_keys=True))'
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($code))
    $raw = & $python -c "import base64;exec(base64.b64decode('$encoded'))"
    if ($LASTEXITCODE -ne 0 -or -not $raw) { throw 'could not inspect live ledger' }
    return $raw | ConvertFrom-Json
}
function Get-StubCalls {
    return @((Invoke-RestMethod -Uri "http://127.0.0.1:$StubPort/calls" -TimeoutSec 15))
}

Assert-PortFree $WorkerPort
Assert-PortFree $StubPort
Assert-PortFree $RunnerBrokerPort
$workflowRoot = Join-Path $runRoot 'workflows'
Copy-Item -LiteralPath (Join-Path $projectRoot 'n8n\workflows') -Destination $workflowRoot -Recurse -Force
$generatePath = Join-Path $workflowRoot 'bidmate_eval_generate_v1.json'
$retryPath = Join-Path $workflowRoot 'bidmate_eval_retry_failed_v1.json'
$idMap = @{
    'bidmate-eval-generate-v1' = 'bm-eval-generate-v1'
    'bidmate-eval-process-v1' = 'bm-eval-process-v1'
    'bidmate-eval-retry-v1' = 'bm-eval-retry-v1'
}
function Set-GenerateBatch([string]$CampaignKey) {
    $generate = Get-Content -Raw -LiteralPath $generatePath | ConvertFrom-Json
    $assignments = ($generate.nodes | Where-Object name -eq 'Batch Config').parameters.assignments.assignments
    $values = @{ target_count = 5; mode = 'live'; campaign_key = $CampaignKey; data_root = 'loopback-stub'; cost_limit_microusd = 5000000; live_authorized = $true }
    foreach ($assignment in $assignments) { if ($values.ContainsKey($assignment.name)) { $assignment.value = $values[$assignment.name] } }
    [IO.File]::WriteAllText($generatePath, ($generate | ConvertTo-Json -Depth 100), [Text.UTF8Encoding]::new($false))
}
function Set-RetryRunId([string]$RunId) {
    $retry = Get-Content -Raw -LiteralPath $retryPath | ConvertFrom-Json
    $assignments = ($retry.nodes | Where-Object name -eq 'Retry Settings').parameters.assignments.assignments
    $runIdAssignment = $assignments | Where-Object name -eq 'run_id'
    if ($null -eq $runIdAssignment) { throw 'Retry Settings run_id assignment is required' }
    $runIdAssignment.value = $RunId
    [IO.File]::WriteAllText($retryPath, ($retry | ConvertTo-Json -Depth 100), [Text.UTF8Encoding]::new($false))
}
Set-GenerateBatch 'loopback-stub-e2e'
foreach ($workflowPath in Get-ChildItem -LiteralPath $workflowRoot -Filter '*.json') {
    $workflowText = [IO.File]::ReadAllText($workflowPath.FullName)
    $workflow = $workflowText.Replace('127.0.0.1:8121', "127.0.0.1:$WorkerPort") | ConvertFrom-Json
    if ($idMap.ContainsKey([string]$workflow.id)) { $workflow.id = $idMap[[string]$workflow.id] }
    foreach ($node in @($workflow.nodes | Where-Object type -eq 'n8n-nodes-base.executeWorkflow')) {
        $target = [string]$node.parameters.workflowId.value
        if ($idMap.ContainsKey($target)) { $node.parameters.workflowId.value = $idMap[$target] }
    }
    if ([string]$workflow.id -eq 'bm-eval-process-v1') {
        foreach ($node in @($workflow.nodes | Where-Object { $_.name -like 'OpenAI *' })) {
            $node.parameters | Add-Member -NotePropertyName authentication -NotePropertyValue 'genericCredentialType' -Force
            $node.parameters | Add-Member -NotePropertyName genericAuthType -NotePropertyValue 'httpHeaderAuth' -Force
            $credential = [pscustomobject]@{
                httpHeaderAuth = [pscustomobject]@{
                    id = 'bidmate-loopback-stub-header'
                    name = 'BidMate Loopback Stub Header'
                }
            }
            $node | Add-Member -NotePropertyName credentials -NotePropertyValue $credential -Force
        }
    }
    [IO.File]::WriteAllText($workflowPath.FullName, ($workflow | ConvertTo-Json -Depth 100), [Text.UTF8Encoding]::new($false))
}

function Import-IsolatedWorkflows([string]$LogStem) {
    Invoke-N8n @('import:workflow','--separate',"--input=$workflowRoot") (Join-Path $runRoot "$LogStem-import.log")
    Invoke-N8n @('publish:workflow','--id=bm-eval-process-v1') (Join-Path $runRoot "$LogStem-publish-process.log")
}

$dataRoot = Join-Path $runRoot 'public-demo\source'
$env:PYTHONPATH = Join-Path $projectRoot 'src'
& $python (Join-Path $projectRoot 'scripts\prepare_public_demo.py') --output (Join-Path $runRoot 'public-demo') --documents 15
if ($LASTEXITCODE -ne 0) { throw 'synthetic public demo preparation failed' }
$manifestSources = @()
foreach ($parsedFile in @(Get-ChildItem -LiteralPath (Join-Path $dataRoot 'Parsed') -Filter '*.json' -File | Sort-Object Name)) {
    $pdfFile = Join-Path (Join-Path $dataRoot 'PDF1') ($parsedFile.BaseName + '.pdf')
    if (-not (Test-Path -LiteralPath $pdfFile)) { throw "synthetic live manifest PDF missing: $pdfFile" }
    $parsedPayload = Get-Content -Raw -LiteralPath $parsedFile.FullName | ConvertFrom-Json
    $manifestSources += [ordered]@{
        source_id = $parsedFile.BaseName
        parsed_file = 'Parsed/' + $parsedFile.Name
        pdf_file = 'PDF1/' + [IO.Path]::GetFileName($pdfFile)
        pdf_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $pdfFile).Hash.ToLowerInvariant()
        page_count = @($parsedPayload.pages).Count
        public_provenance_checked = $true
        empty_pages_within_threshold = $true
    }
}
if ($manifestSources.Count -ne 15) { throw 'synthetic live manifest must contain 15 documents' }
[IO.File]::WriteAllText(
    (Join-Path $dataRoot 'runtime-manifest.json'),
    ([ordered]@{ schema_version = 1; sources = $manifestSources } | ConvertTo-Json -Depth 8),
    [Text.UTF8Encoding]::new($false)
)
$env:PYTHONUNBUFFERED = '1'
$env:BIDMATE_EVAL_MODE = 'live'
$env:BIDMATE_EVAL_AUTOMATION_LEDGER = Join-Path $runRoot 'automation\ledger.sqlite3'
$env:BIDMATE_EVAL_AUTOMATION_PACKAGE = Join-Path $runRoot 'automation\candidate-package'
$env:BIDMATE_EVAL_BATCH_CONFIG = Join-Path $dataRoot 'Batch_config.json'
$env:BIDMATE_EVAL_JSON_ROOT = Join-Path $dataRoot 'Parsed'
$env:BIDMATE_EVAL_PDF_ROOT = Join-Path $dataRoot 'PDF1'
$env:BIDMATE_EVAL_INVENTORY_CACHE = Join-Path $runRoot 'automation\cache'
$env:BIDMATE_OPENAI_BASE_URL = "http://127.0.0.1:$StubPort/v1"
$env:BIDMATE_EVAL_STUB_MODE = 'true'
$env:BIDMATE_STUB_EVENT_LOG = Join-Path $runRoot 'stub-calls.json'
$env:N8N_USER_FOLDER = Join-Path $runRoot 'n8n-user'
$env:N8N_ENCRYPTION_KEY = [guid]::NewGuid().ToString('N')
$env:N8N_DIAGNOSTICS_ENABLED = 'false'
$env:N8N_VERSION_NOTIFICATIONS_ENABLED = 'false'
$env:N8N_TEMPLATES_ENABLED = 'false'
$env:N8N_LOG_LEVEL = 'info'
Remove-Item Env:N8N_RUNNERS_ENABLED -ErrorAction SilentlyContinue
$env:N8N_RUNNERS_MODE = 'internal'
$env:N8N_RUNNERS_BROKER_PORT = "$RunnerBrokerPort"
$env:N8N_RUNNERS_BROKER_LISTEN_ADDRESS = '127.0.0.1'
New-Item -ItemType Directory -Force -Path $env:N8N_USER_FOLDER | Out-Null
$stubCredentialPath = Join-Path $runRoot 'stub-http-header-auth.json'
$stubCredential = @([ordered]@{
    id = 'bidmate-loopback-stub-header'
    name = 'BidMate Loopback Stub Header'
    type = 'httpHeaderAuth'
    data = [ordered]@{ name = 'Authorization'; value = 'Bearer stub-only' }
})
$stubCredentialJson = '[' + ($stubCredential | ConvertTo-Json -Depth 8 -Compress) + ']'
[IO.File]::WriteAllText($stubCredentialPath, $stubCredentialJson, [Text.UTF8Encoding]::new($false))

$stub = $null
$worker = $null
$publicationCheckPath = $null
try {
    $stub = Start-Process -FilePath $python -ArgumentList @('-m','uvicorn','live_responses_stub:app','--app-dir',(Join-Path $projectRoot 'tests\eval_dataset'),'--host','127.0.0.1','--port',"$StubPort") -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runRoot 'stub.stdout.log') -RedirectStandardError (Join-Path $runRoot 'stub.stderr.log') -PassThru
    $stubHealth = Wait-Ready "http://127.0.0.1:$StubPort/health" 'loopback Responses stub' $stub
    if ($stubHealth.status -ne 'ready') { throw 'stub health contract failed' }
    $worker = Start-Process -FilePath $python -ArgumentList @('-m','uvicorn','bidmate_rag.eval_dataset.automation.api:app','--host','127.0.0.1','--port',"$WorkerPort") -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runRoot 'worker.stdout.log') -RedirectStandardError (Join-Path $runRoot 'worker.stderr.log') -PassThru
    $workerHealth = Wait-Ready "http://127.0.0.1:$WorkerPort/v1/health" 'live automation worker' $worker
    if ($workerHealth.status -ne 'ready') { throw 'worker health contract failed' }
    Invoke-N8n @('import:credentials',"--input=$stubCredentialPath") (Join-Path $runRoot 'import-stub-credential.log')
    Import-IsolatedWorkflows 'initial'
    $publicationCheckPath = Join-Path $runRoot 'verify-publication.py'
    $publicationLines = @(
        'import sqlite3',
        'import sys',
        'connection = sqlite3.connect(sys.argv[1])',
        'row = connection.execute("SELECT active, activeVersionId, versionId FROM workflow_entity WHERE id=?", ("bm-eval-process-v1",)).fetchone()',
        'history = connection.execute("SELECT COUNT(*) FROM workflow_history WHERE workflowId=?", ("bm-eval-process-v1",)).fetchone()[0]',
        'assert row and row[0] == 1 and row[1] == row[2] and history >= 1, (row, history)'
    )
    [IO.File]::WriteAllLines($publicationCheckPath, [string[]]$publicationLines, [Text.UTF8Encoding]::new($false))
    $publicationOutput = @(& $python $publicationCheckPath (Join-Path $env:N8N_USER_FOLDER '.n8n\database.sqlite') 2>&1)
    $publicationExit = $LASTEXITCODE
    [IO.File]::WriteAllLines((Join-Path $runRoot 'publish-process-check.log'), [string[]]$publicationOutput, [Text.UTF8Encoding]::new($false))
    if ($publicationExit -ne 0) { throw 'isolated process workflow publication verification failed' }
    Invoke-N8n @('execute','--id=bm-eval-generate-v1','--rawOutput') (Join-Path $runRoot 'live-first.log')
    $firstState = Get-LedgerState
    $calls = Get-Content -Raw -LiteralPath $env:BIDMATE_STUB_EVENT_LOG | ConvertFrom-Json
    $stageCounts = @{}
    foreach ($stage in 'selector','generator','reviewer') { $stageCounts[$stage] = @($calls | Where-Object stage -eq $stage).Count }
    if ($firstState.done_count -ne 5 -or $firstState.candidate_count -ne 5 -or $stageCounts.selector -ne 5 -or $stageCounts.generator -ne 5 -or $stageCounts.reviewer -ne 5) {
        throw "live path did not complete exactly five selector/generator/reviewer calls: $($stageCounts | ConvertTo-Json -Compress)"
    }
    $callsBefore = @($calls).Count
    Invoke-N8n @('execute','--id=bm-eval-generate-v1','--rawOutput') (Join-Path $runRoot 'live-idempotent.log')
    $callsAfter = @((Get-Content -Raw -LiteralPath $env:BIDMATE_STUB_EVENT_LOG | ConvertFrom-Json)).Count
    $finalState = Get-LedgerState
    if ($callsAfter -ne $callsBefore -or $finalState.done_count -ne 5) { throw 'idempotent live rerun added provider calls or changed terminal state' }
    function Invoke-GraphFailureScenario([string]$CampaignKey, [string[]]$Scenarios, [string]$LogStem) {
        Set-GenerateBatch $CampaignKey
        Import-IsolatedWorkflows $LogStem
        Set-StubScenarioPlan @($Scenarios) | Out-Null
        Invoke-N8n @('execute','--id=bm-eval-generate-v1','--rawOutput') (Join-Path $runRoot "$LogStem-generate.log")
        return Get-LedgerState
    }
    function Invoke-GraphRetry([string]$RunId, [string]$LogStem) {
        Set-RetryRunId $RunId
        Import-IsolatedWorkflows "$LogStem-retry"
        Set-StubScenarioPlan @() | Out-Null
        Invoke-N8n @('execute','--id=bm-eval-retry-v1','--rawOutput') (Join-Path $runRoot "$LogStem-retry.log")
        return Get-LedgerState
    }

    $rateState = Invoke-GraphFailureScenario 'loopback-stub-rate-e2e' 'rate_limited' 'rate-limited'
    if ($rateState.retryable_count -ne 1 -or $rateState.done_count -ne 4) { throw 'n8n 429 error output did not create exactly one repair' }
    $rateRepaired = Invoke-GraphRetry $rateState.run_id 'rate-limited'
    if ($rateRepaired.retryable_count -ne 0 -or $rateRepaired.done_count -ne 5) { throw 'n8n 429 repair did not complete exactly once' }
    $serverState = Invoke-GraphFailureScenario 'loopback-stub-server-e2e' 'transient_server' 'transient-server'
    if ($serverState.retryable_count -ne 1 -or $serverState.done_count -ne 4) { throw 'n8n 5xx error output did not create exactly one repair' }
    $serverRepaired = Invoke-GraphRetry $serverState.run_id 'transient-server'
    if ($serverRepaired.retryable_count -ne 0 -or $serverRepaired.done_count -ne 5) { throw 'n8n 5xx repair did not complete exactly once' }

    $generatorCallsBefore = @(Get-StubCalls).Count
    $generatorState = Invoke-GraphFailureScenario 'loopback-stub-generator-retry-e2e' @('success','rate_limited') 'generator-rate-limited'
    if ($generatorState.retryable_count -ne 1 -or $generatorState.done_count -ne 4) { throw 'generator-stage 429 did not leave exactly one retryable unit' }
    $generatorRepaired = Invoke-GraphRetry $generatorState.run_id 'generator-rate-limited'
    if ($generatorRepaired.retryable_count -ne 0 -or $generatorRepaired.done_count -ne 5) { throw 'generator-stage retry did not complete' }
    $generatorCalls = @((Get-StubCalls) | Select-Object -Skip $generatorCallsBefore)
    if (@($generatorCalls | Where-Object stage -eq 'selector').Count -ne 5 -or @($generatorCalls | Where-Object stage -eq 'generator').Count -ne 6 -or @($generatorCalls | Where-Object stage -eq 'reviewer').Count -ne 5) {
        throw 'generator-stage retry reissued an earlier selector stage or missed resumed stages'
    }
    $generatorRetried = @($generatorCalls | Group-Object work_unit_id | Where-Object { @($_.Group | Where-Object stage -eq 'generator').Count -eq 2 })
    $generatorRetryKeys = @($generatorRetried[0].Group | Where-Object stage -eq 'generator' | Select-Object -ExpandProperty idempotency_key -Unique)
    if ($generatorRetried.Count -ne 1 -or $generatorRetryKeys.Count -ne 1) { throw 'generator 429 retry must preserve one idempotency key' }

    $reviewerCallsBefore = @(Get-StubCalls).Count
    $reviewerState = Invoke-GraphFailureScenario 'loopback-stub-reviewer-retry-e2e' @('success','success','transient_server') 'reviewer-server'
    if ($reviewerState.retryable_count -ne 1 -or $reviewerState.done_count -ne 4) { throw 'reviewer-stage 5xx did not leave exactly one retryable unit' }
    $reviewerRepaired = Invoke-GraphRetry $reviewerState.run_id 'reviewer-server'
    if ($reviewerRepaired.retryable_count -ne 0 -or $reviewerRepaired.done_count -ne 5) { throw 'reviewer-stage retry did not complete' }
    $reviewerCalls = @((Get-StubCalls) | Select-Object -Skip $reviewerCallsBefore)
    if (@($reviewerCalls | Where-Object stage -eq 'selector').Count -ne 5 -or @($reviewerCalls | Where-Object stage -eq 'generator').Count -ne 5 -or @($reviewerCalls | Where-Object stage -eq 'reviewer').Count -ne 6) {
        throw 'reviewer-stage retry reissued selector or generator'
    }
    $reviewerRetried = @($reviewerCalls | Group-Object work_unit_id | Where-Object { @($_.Group | Where-Object stage -eq 'reviewer').Count -eq 2 })
    $reviewerRetryKeys = @($reviewerRetried[0].Group | Where-Object stage -eq 'reviewer' | Select-Object -ExpandProperty idempotency_key -Unique)
    if ($reviewerRetried.Count -ne 1 -or $reviewerRetryKeys.Count -ne 1) { throw 'reviewer 5xx retry must preserve one idempotency key' }

    $invalidCallsBefore = @(Get-StubCalls).Count
    $invalidState = Invoke-GraphFailureScenario 'loopback-stub-invalid-e2e' 'invalid_structured' 'invalid-structured'
    if ($invalidState.retryable_count -ne 1 -or $invalidState.done_count -ne 4) { throw 'n8n normalize error output did not create one structured repair' }
    $invalidRepaired = Invoke-GraphRetry $invalidState.run_id 'invalid-structured'
    if ($invalidRepaired.retryable_count -ne 0 -or $invalidRepaired.done_count -ne 5) { throw 'n8n structured repair did not complete exactly once' }
    $invalidCalls = @((Get-StubCalls) | Select-Object -Skip $invalidCallsBefore)
    $invalidInitialCall = $invalidCalls[0]
    $invalidSelectorCalls = @($invalidCalls | Where-Object { $_.stage -eq 'selector' -and $_.work_unit_id -eq $invalidInitialCall.work_unit_id })
    if ($invalidSelectorCalls.Count -ne 2 -or $invalidSelectorCalls[0].idempotency_key -eq $invalidSelectorCalls[1].idempotency_key) {
        throw 'structured repair did not change the request body and idempotency key'
    }

    $reviewRepairCallsBefore = @(Get-StubCalls).Count
    $reviewRepairState = Invoke-GraphFailureScenario 'loopback-stub-review-repair-e2e' @('success','success','review_repair') 'review-repair'
    if ($reviewRepairState.retryable_count -ne 0 -or $reviewRepairState.done_count -ne 5) { throw 'reviewer-directed generator repair did not complete inline' }
    $reviewRepairCalls = @((Get-StubCalls) | Select-Object -Skip $reviewRepairCallsBefore)
    if (@($reviewRepairCalls | Where-Object stage -eq 'selector').Count -ne 5 -or @($reviewRepairCalls | Where-Object stage -eq 'generator').Count -ne 6 -or @($reviewRepairCalls | Where-Object stage -eq 'reviewer').Count -ne 6) {
        throw 'reviewer-directed repair did not run exactly one extra generator and reviewer call'
    }

    $ambiguousState = Invoke-GraphFailureScenario 'loopback-stub-ambiguous-e2e' 'ambiguous' 'ambiguous'
    $costsAfterAmbiguous = Invoke-RestMethod -Uri "http://127.0.0.1:$WorkerPort/v1/runs/$($ambiguousState.run_id)/costs" -TimeoutSec 15
    if ($ambiguousState.retryable_count -ne 0 -or $ambiguousState.permanent_failed_count -ne 1 -or $costsAfterAmbiguous.open_reserved_microusd -le 0) { throw 'n8n ambiguous transport error output did not retain unknown reservation with zero retry' }

    $capRun = Invoke-JsonPost "http://127.0.0.1:$WorkerPort/v1/runs" @{
        batch_id = 1; target_count = 5; mode = 'live'; campaign_key = 'loopback-stub-cap-e2e'; data_root = 'loopback-stub'; cost_limit_microusd = 5000000; live_authorized = $true
    }
    $capRunId = $capRun.run_id
    $capPlan = (Invoke-JsonPost "http://127.0.0.1:$WorkerPort/v1/runs/$capRunId/plan" @{}).work_units
    $capUnit = $capPlan[0]
    $capSeed = "import base64,os; from bidmate_rag.eval_dataset.automation.ledger import AutomationLedger; ledger=AutomationLedger(os.environ['BIDMATE_EVAL_AUTOMATION_LEDGER']); call=ledger.reserve_provider_call(run_id='$capRunId', work_unit_id='$($capUnit.work_unit_id)', stage='cap_seed', attempt=1, model='stub', request_hash='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', reserved_microusd=4500000); ledger.reconcile_provider_call(provider_call_id=call.provider_call_id, status='succeeded', actual_microusd=4500000, input_tokens=0, output_tokens=0, provider_response_id='cap-seed')"
    $capEncoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($capSeed))
    & $python -c "import base64;exec(base64.b64decode('$capEncoded'))"
    if ($LASTEXITCODE -ne 0) { throw 'could not seed conservative operational cap' }
    $stubCallsBeforeCap = @((Invoke-RestMethod -Uri "http://127.0.0.1:$StubPort/calls" -TimeoutSec 15)).Count
    $capRejected = $false
    try { Invoke-JsonPost "http://127.0.0.1:$WorkerPort/v1/work-units/$($capUnit.work_unit_id)/selector/prepare" @{ attempt = 1 } | Out-Null } catch { $capRejected = $true }
    $stubCallsAfterCap = @((Invoke-RestMethod -Uri "http://127.0.0.1:$StubPort/calls" -TimeoutSec 15)).Count
    if (-not $capRejected -or $stubCallsAfterCap -ne $stubCallsBeforeCap) { throw 'operational cap did not stop before a provider call' }
    $evidence = [ordered]@{ status='PASS'; n8n_version=$n8nVersion; run_id=$finalState.run_id; selector_calls=$stageCounts.selector; generator_calls=$stageCounts.generator; reviewer_calls=$stageCounts.reviewer; idempotent_new_calls=($callsAfter-$callsBefore); rate_limited_retries=1; transient_server_retries=1; generator_stage_retries=1; reviewer_stage_retries=1; structured_output_repairs=1; reviewer_directed_repairs=1; ambiguous_retryable=$ambiguousState.retryable_count; ambiguous_open_reserved_microusd=$costsAfterAmbiguous.open_reserved_microusd; insufficient_cap_provider_calls=($stubCallsAfterCap-$stubCallsBeforeCap); run_root=$runRoot }
    [IO.File]::WriteAllText((Join-Path $runRoot 'live-stub-evidence.json'), ($evidence | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
    Write-Output ($evidence | ConvertTo-Json -Depth 8)
    Write-Output 'LIVE_STUB_E2E=PASS'
}
finally {
    if ($null -ne $publicationCheckPath -and (Test-Path -LiteralPath $publicationCheckPath)) {
        $checkPath = (Resolve-Path -LiteralPath $publicationCheckPath).Path
        if (-not $checkPath.StartsWith($runRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw "refusing to delete publication check outside RunRoot: $checkPath"
        }
        Remove-Item -LiteralPath $checkPath -Force
    }
    foreach ($process in @($worker, $stub)) { if ($null -ne $process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force; $process.WaitForExit(5000) | Out-Null } }
    foreach ($port in $WorkerPort,$StubPort,$RunnerBrokerPort) { if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) { throw "E2E cleanup left port listening: $port" } }
}