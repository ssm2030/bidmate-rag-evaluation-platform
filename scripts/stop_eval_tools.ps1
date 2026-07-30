param(
    [ValidateSet("generator", "reviewer", "all")]
    [string]$Stack = "all"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtimeRoot = Join-Path $projectRoot "artifacts\eval_dataset\rebuild\runtime"
$statePath = Join-Path $runtimeRoot "processes.json"
if (-not (Test-Path -LiteralPath $statePath)) {
    Write-Host "BidMate eval tool state is not present; nothing to stop."
    exit 0
}
$state = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
$remaining = [Collections.Generic.List[object]]::new()
$stopped = [Collections.Generic.List[string]]::new()

$orderedProcesses = @($state.processes)
[array]::Reverse($orderedProcesses)
foreach ($record in $orderedProcesses) {
    if ($Stack -ne "all" -and $record.stack -ne $Stack) {
        $remaining.Insert(0, $record)
        continue
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($record.pid)" -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        continue
    }
    $commandLine = [string]$process.CommandLine
    $listener = Get-NetTCPConnection -State Listen -LocalAddress "127.0.0.1" -LocalPort ([int]$record.port) -ErrorAction SilentlyContinue |
        Where-Object OwningProcess -eq ([int]$record.pid) |
        Select-Object -First 1
    if ($commandLine.IndexOf([string]$record.command_token, [StringComparison]::OrdinalIgnoreCase) -lt 0 -or $null -eq $listener) {
        Write-Warning "refusing to stop $($record.name): PID, command line, and loopback port do not all match"
        $remaining.Insert(0, $record)
        continue
    }
    Stop-Process -Id ([int]$record.pid) -Force
    $stopped.Add([string]$record.name)
}

if ($remaining.Count -eq 0) {
    [IO.File]::Delete($statePath)
} else {
    $nextState = [ordered]@{
        started_at = $state.started_at
        stack = if ($remaining.Count -eq 1) { $remaining[0].stack } else { "partial" }
        processes = @($remaining)
    }
    [IO.File]::WriteAllText($statePath, ($nextState | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
}
Write-Host ("stopped: " + ($(if ($stopped.Count) { $stopped -join ", " } else { "none" })))