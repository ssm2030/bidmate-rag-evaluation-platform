$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$rebuildRoot = [IO.Path]::GetFullPath((Join-Path $root "artifacts\eval_dataset\rebuild"))
$finalRoot = Join-Path $rebuildRoot "verification\final"
$python = Join-Path $root ".venv\Scripts\python.exe"
$approvalCommand = "APPROVE build-bidmate-live-eval-poc-amendment-04-xhigh-v06 b8bfa69619480c0f99007a1f4fa253f74b56988b34dbd40c28bea8733b84710e"
$packetPath = Join-Path $root "APPROVED_IMPLEMENTATION_PACKET.amendment-04.json"
$manualRoot = Join-Path $root "artifacts\eval_dataset\manual"
$manualArtifactsPresentBefore = Test-Path -LiteralPath $manualRoot
$personalWork = -join [char[]](0xAC1C, 0xC778, 0x20, 0xC791, 0xC5C5)
$harnessScripts = Join-Path $env:USERPROFILE ("Desktop\{0}\AI-Harness-System\scripts" -f $personalWork)
$overall = [Diagnostics.Stopwatch]::StartNew()
$generatorElapsed = 0.0
$reviewElapsed = 0.0
$launcherElapsed = 0.0
$liveStubElapsed = 0.0

Push-Location $root
try {
    New-Item -ItemType Directory -Force -Path $finalRoot | Out-Null
    $packetOutput = @(& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $harnessScripts "Test-ApprovedImplementationPacket.ps1") -PacketPath $packetPath -ProjectRoot $root -Mode Final -ApprovalCommand $approvalCommand -Quiet 2>&1)
    if ($LASTEXITCODE -ne 0 -or ($packetOutput -join "`n") -notmatch "PACKET_STATUS=PASS") {
        throw "Final packet validation failed: $($packetOutput -join ' ')"
    }

    $publicationPolicyPath = Join-Path $root "configs\publication\public_snapshot.json"
    $publicationPolicy = Get-Content -Raw -LiteralPath $publicationPolicyPath | ConvertFrom-Json
    $presentPromptPaths = @($publicationPolicy.prompt_paths | Where-Object { Test-Path -LiteralPath (Join-Path $root $_) })
    $promptCount = $presentPromptPaths.Count
    if ($promptCount -ne [int]$publicationPolicy.expected_prompt_count -or $promptCount -gt [int]$publicationPolicy.max_prompt_count) {
        throw "prompt count gate failed: expected=$($publicationPolicy.expected_prompt_count), max=$($publicationPolicy.max_prompt_count), actual=$promptCount"
    }

    $trackedPaths = @(git -c core.quotepath=false ls-files)
    if ($LASTEXITCODE -ne 0) { throw "tracked-path inventory failed" }
    $trackedForbidden = [Collections.Generic.List[string]]::new()
    $trackedLarge = [Collections.Generic.List[string]]::new()
    foreach ($trackedPath in $trackedPaths) {
        $trackedFullPath = Join-Path $root $trackedPath
        if (-not (Test-Path -LiteralPath $trackedFullPath -PathType Leaf)) { continue }
        $trackedSuffix = [IO.Path]::GetExtension($trackedPath).ToLowerInvariant()
        if (@($publicationPolicy.forbidden_extensions) -contains $trackedSuffix) { $trackedForbidden.Add($trackedPath) }
        if ((Get-Item -LiteralPath $trackedFullPath).Length -gt [int64]$publicationPolicy.max_file_bytes) { $trackedLarge.Add($trackedPath) }
    }
    if ($trackedForbidden.Count -gt 0) { throw "tracked forbidden artifacts detected: $($trackedForbidden -join ", ")" }
    if ($trackedLarge.Count -gt 0) { throw "tracked files exceed max_file_bytes: $($trackedLarge -join ", ")" }

    $publicationWorktreeArgs = @("--root", ".", "--scope", "worktree")
    $publicationWorktreeOutput = @(& $python -m scripts.publication.verify_public_snapshot @publicationWorktreeArgs 2>&1)
    $publicationWorktreeExit = $LASTEXITCODE
    $allowedImmutablePublicationFindings = @(
        "FINDING absolute-user-path STATE.md matched a prohibited text pattern",
        "FINDING absolute-user-path WORK_PLAN.md matched a prohibited text pattern",
        "FINDING absolute-user-path docs/live-eval-poc-design.md matched a prohibited text pattern",
        "FINDING email-address docs/superpowers/plans/2026-07-31-bidmate-live-eval-poc.md matched a prohibited text pattern"
    ) | Sort-Object
    [string[]]$actualPublicationFindings = @($publicationWorktreeOutput | Where-Object { $_ -like "FINDING *" } | Sort-Object)
    $publicationCleanPass = $publicationWorktreeExit -eq 0 -and
        $actualPublicationFindings.Count -eq 0 -and
        ($publicationWorktreeOutput -join "`n") -match "PUBLICATION_SAFETY_STATUS=PASS"
    $publicationAllowlistPass = $false
    if ($actualPublicationFindings.Count -gt 0) {
        $publicationDifference = @(Compare-Object -ReferenceObject $allowedImmutablePublicationFindings -DifferenceObject $actualPublicationFindings)
        $publicationAllowlistPass = $publicationWorktreeExit -eq 1 -and $publicationDifference.Count -eq 0
    }
    if (-not $publicationCleanPass -and -not $publicationAllowlistPass) {
        throw "worktree publication scan differs from the exact manifested immutable allowlist: $($publicationWorktreeOutput -join ' | ')"
    }
    $publicationObjectArgs = @("--root", ".", "--scope", "objects")
    $publicationObjectOutput = @(& $python -m scripts.publication.verify_public_snapshot @publicationObjectArgs 2>&1)
    if ($LASTEXITCODE -ne 0 -or ($publicationObjectOutput -join "`n") -notmatch "PUBLICATION_SAFETY_STATUS=PASS") {
        throw "Git object publication scan failed: $($publicationObjectOutput -join ' | ')"
    }

    $secretHits = @(rg -n --glob "!artifacts/**" --glob "!*.lock" "(?i)(sk-[a-z0-9]{20,}|bearer\s+[a-z0-9._-]{12,})" src configs prompts n8n scripts web 2>$null)
    if ($LASTEXITCODE -eq 0 -and $secretHits.Count -gt 0) { throw "secret-like delivery content detected: $($secretHits[0])" }
    if ($LASTEXITCODE -gt 1) { throw "secret scan failed" }

    $expectedNodes = [ordered]@{
        "n8n/workflows/bidmate_eval_generate_v1.json" = 17
        "n8n/workflows/bidmate_eval_process_work_unit_v1.json" = 33
        "n8n/workflows/bidmate_eval_retry_failed_v1.json" = 8
    }
    foreach ($entry in $expectedNodes.GetEnumerator()) {
        $workflow = Get-Content -Raw -LiteralPath $entry.Key | ConvertFrom-Json
        if (@($workflow.nodes).Count -ne $entry.Value) {
            throw "workflow node count mismatch for $($entry.Key): expected $($entry.Value), found $(@($workflow.nodes).Count)"
        }
        foreach ($node in @($workflow.nodes)) {
            if ($null -ne $node.PSObject.Properties["credentials"] -and $null -ne $node.credentials) {
                throw "workflow contains tracked credentials: $($entry.Key) node=$($node.name)"
            }
        }
        $workflowText = Get-Content -Raw -LiteralPath $entry.Key
        $remoteUrls = @([regex]::Matches($workflowText, "https?://(?!127\.0\.0\.1)"))
        if ($remoteUrls.Count -gt 0) { throw "workflow contains a non-loopback HTTP endpoint: $($entry.Key)" }
    }

    $testRun = Join-Path $rebuildRoot (".tmp\tests\delivery-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $testRun | Out-Null
    $env:TEMP = $testRun
    $env:TMP = $testRun
    $pytestOutput = @(& $python -m pytest --import-mode=importlib -q tests/eval_dataset tests/unit/test_eval_correctness.py tests/unit/test_schema_validator.py tests/unit/test_eval_ui_readonly.py --basetemp (Join-Path $testRun "pytest") 2>&1)
    $pytestExit = $LASTEXITCODE
    $pytestOutput | Write-Output
    if ($pytestExit -ne 0) { throw "Python regression suite failed" }
    $pytestText = $pytestOutput -join "`n"
    $pytestMatch = [regex]::Match($pytestText, "(\d+) passed")
    if (-not $pytestMatch.Success) { throw "pytest pass count was not found" }
    $pytestPassed = [int]$pytestMatch.Groups[1].Value

    uv run ruff check src/bidmate_rag/eval_dataset tests/eval_dataset app/eval_ui.py src/bidmate_rag/evaluation/dataset.py src/bidmate_rag/evaluation/schema_validator.py tests/unit/test_eval_correctness.py tests/unit/test_eval_ui_readonly.py tests/unit/test_schema_validator.py
    if ($LASTEXITCODE -ne 0) { throw "Ruff validation failed" }

    $previousN8nBlockEnvAccess = [Environment]::GetEnvironmentVariable("N8N_BLOCK_ENV_ACCESS_IN_NODE")
    try {
        $env:N8N_BLOCK_ENV_ACCESS_IN_NODE = 'false'
        $generatorTimer = [Diagnostics.Stopwatch]::StartNew()
        & powershell -NoProfile -ExecutionPolicy Bypass -File scripts\test_eval_automation_mock.ps1
        if ($LASTEXITCODE -ne 0) { throw "n8n generator integration failed" }
        $generatorTimer.Stop()
        $generatorElapsed = [math]::Round($generatorTimer.Elapsed.TotalSeconds, 3)
    } finally {
        if ($null -eq $previousN8nBlockEnvAccess) {
            Remove-Item Env:N8N_BLOCK_ENV_ACCESS_IN_NODE -ErrorAction SilentlyContinue
        } else {
            $env:N8N_BLOCK_ENV_ACCESS_IN_NODE = $previousN8nBlockEnvAccess
        }
    }
    if ($env:N8N_BLOCK_ENV_ACCESS_IN_NODE -eq 'false') {
        throw "full verifier requires n8n environment access to remain blocked outside the mock child"
    }

    $liveStubTimer = [Diagnostics.Stopwatch]::StartNew()
    & powershell -NoProfile -ExecutionPolicy Bypass -File scripts\test_eval_automation_live_stub.ps1
    if ($LASTEXITCODE -ne 0) { throw 'n8n live-stub E2E failed' }
    $liveStubTimer.Stop()
    $liveStubElapsed = [math]::Round($liveStubTimer.Elapsed.TotalSeconds, 3)

    npm --prefix web run lint
    if ($LASTEXITCODE -ne 0) { throw "review web lint failed" }

    $reviewTimer = [Diagnostics.Stopwatch]::StartNew()
    & powershell -NoProfile -ExecutionPolicy Bypass -File scripts\test_eval_review_e2e.ps1
    if ($LASTEXITCODE -ne 0) { throw "actual-package review E2E failed" }
    $reviewTimer.Stop()
    $reviewElapsed = [math]::Round($reviewTimer.Elapsed.TotalSeconds, 3)

    Remove-Item Env:EVAL_REVIEW_API_ORIGIN -ErrorAction SilentlyContinue
    Remove-Item Env:EVAL_REVIEW_BACKEND_ORIGIN -ErrorAction SilentlyContinue
    Remove-Item Env:EVAL_REVIEW_E2E_PORT -ErrorAction SilentlyContinue
    npm --prefix web run build
    if ($LASTEXITCODE -ne 0) { throw "default review web production build failed" }

    $launcherTimer = [Diagnostics.Stopwatch]::StartNew()
    $launcherStarted = $false
    try {
        & powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_eval_tools.ps1 -Stack all
        if ($LASTEXITCODE -ne 0) { throw "eval tool launcher failed" }
        $launcherStarted = $true
        $statePath = Join-Path $rebuildRoot "runtime\processes.json"
        $state = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
        if (@($state.processes).Count -ne 4) { throw "launcher did not record four processes" }
        $health = [ordered]@{
            worker = (Invoke-RestMethod "http://127.0.0.1:8121/v1/health" -TimeoutSec 3).status
            n8n = (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:5678/healthz" -TimeoutSec 3).StatusCode
            review_api = (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8101/api/packages" -TimeoutSec 3).StatusCode
            review_web = (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:3000/eval-review" -TimeoutSec 3).StatusCode
        }
        if ($health.worker -ne "ready" -or $health.n8n -ne 200 -or $health.review_api -ne 200 -or $health.review_web -ne 200) {
            throw "launcher health verification failed"
        }
    } finally {
        if ($launcherStarted -or (Test-Path -LiteralPath (Join-Path $rebuildRoot "runtime\processes.json"))) {
            & powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop_eval_tools.ps1 -Stack all
            if ($LASTEXITCODE -ne 0) { throw "eval tool stop launcher failed" }
        }
    }
    $launcherTimer.Stop()
    $launcherElapsed = [math]::Round($launcherTimer.Elapsed.TotalSeconds, 3)
    foreach ($port in 5678, 8121, 8101, 3000) {
        if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
            throw "launcher left port $port listening"
        }
    }

    $protectedPath = Join-Path $root "src\bidmate_rag\storage\metadata_store.py"
    $protected = (Get-FileHash -LiteralPath $protectedPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($protected -ne "158d69a061dc11bc7d41fdcefe10d7604060b2dfec4909a5fcc51c93cc6cdcef") {
        throw "protected metadata_store.py hash changed"
    }
    git diff --quiet HEAD -- src/bidmate_rag/storage/metadata_store.py
    if ($LASTEXITCODE -ne 0) { throw "branch content changed protected metadata_store.py" }
    if ($manualArtifactsPresentBefore -and -not (Test-Path -LiteralPath $manualRoot)) {
        throw "pre-existing manual artifacts are missing"
    }
    git diff --check
    if ($LASTEXITCODE -ne 0) { throw "git diff whitespace validation failed" }

    $generatorEvidencePath = Join-Path $rebuildRoot "verification\n8n-mock\generator-evidence.json"
    $reviewEvidencePath = Join-Path $rebuildRoot "verification\review-e2e\summary.json"
    $generatorEvidence = Get-Content -Raw -LiteralPath $generatorEvidencePath | ConvertFrom-Json
    $reviewEvidence = Get-Content -Raw -LiteralPath $reviewEvidencePath | ConvertFrom-Json
    if ($generatorEvidence.manifest_sha256 -ne $reviewEvidence.package_manifest_sha256) {
        throw "generator and reviewer did not use the same package manifest"
    }
    $runtimeBytes = (Get-ChildItem -LiteralPath $rebuildRoot -Recurse -File -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
    if ($runtimeBytes -gt 300MB) { throw "approved rebuild runtime exceeded 300 MiB" }

    $overall.Stop()
    $summary = [ordered]@{
        status = "PASS"
        packet_id = "build-bidmate-live-eval-poc-amendment-04-xhigh-v06"
        packet_content_hash = "b8bfa69619480c0f99007a1f4fa253f74b56988b34dbd40c28bea8733b84710e"
        pytest_passed = $pytestPassed
        workflow_nodes = [ordered]@{ generate = 17; process = 33; retry = 8 }
        prompt_count = $promptCount
        prompt_limit = [int]$publicationPolicy.max_prompt_count
        tracked_forbidden_artifacts = $trackedForbidden.Count
        tracked_large_files = $trackedLarge.Count
        workflow_auth_leakage = 0
        publication_worktree = "pass_with_manifested_immutable_allowlist"
        publication_git_objects = "pass"
        generator_seconds = $generatorElapsed
        live_stub_e2e = "pass"
        live_stub_seconds = $liveStubElapsed
        review_e2e_seconds = $reviewElapsed
        launcher_seconds = $launcherElapsed
        total_seconds = [math]::Round($overall.Elapsed.TotalSeconds, 3)
        runtime_bytes = [int64]$runtimeBytes
        package_manifest_sha256 = $generatorEvidence.manifest_sha256
        item_count = $generatorEvidence.items
        anchor_count = $generatorEvidence.anchors
        retry_count = $generatorEvidence.retry_count
        duplicate_provider_calls = $generatorEvidence.duplicateProviderCalls
        manifest_hash_stable = $generatorEvidence.manifestHashStable
        standard_count = $reviewEvidence.standard_count
        safety_count = $reviewEvidence.safety_count
        approved_count = $reviewEvidence.approved_count
        rejected_count = $reviewEvidence.rejected_count
        browser_external_requests = $reviewEvidence.browser_external_requests
        protected_sha256 = $protected
        live_poc = "separately_gated_not_run"
    }
    $summaryPath = Join-Path $finalRoot "verification-summary.json"
    [IO.File]::WriteAllText($summaryPath, ($summary | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
    Write-Output ($summary | ConvertTo-Json -Depth 8)
    Write-Output "DELIVERY_VERIFICATION=PASS"
} finally {
    Pop-Location
}