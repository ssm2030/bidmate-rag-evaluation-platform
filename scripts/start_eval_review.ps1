param(
    [string]$DatabasePath = "artifacts/eval_dataset/rebuild/review/review.sqlite3",
    [string]$PackageRoot = "artifacts/eval_dataset/rebuild/automation",
    [string]$ExportRoot = "artifacts/eval_dataset/rebuild/review/exports",
    [string]$PdfRoot = "",
    [int]$Port = 8101
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$dataRoot = Join-Path $projectRoot "artifacts\public_demo\source"
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "project Python is missing: $python" }
$env:PYTHONPATH = Join-Path $projectRoot "src"
if (-not $PdfRoot) {
    $PdfRoot = Join-Path $dataRoot "PDF1"
    if (-not (Test-Path -LiteralPath $PdfRoot)) {
        & $python (Join-Path $projectRoot "scripts\prepare_public_demo.py") --output (Join-Path $projectRoot "artifacts\public_demo") --documents 15
        if ($LASTEXITCODE -ne 0) { throw "public synthetic demo preparation failed" }
    }
} elseif (-not [IO.Path]::IsPathRooted($PdfRoot)) {
    $PdfRoot = Join-Path $projectRoot $PdfRoot
}
if (-not (Test-Path -LiteralPath $PdfRoot)) { throw "review PDF root is missing: $PdfRoot" }
$env:BIDMATE_EVAL_REVIEW_DB = [IO.Path]::GetFullPath((Join-Path $projectRoot $DatabasePath))
$env:BIDMATE_EVAL_PACKAGE_ROOT = [IO.Path]::GetFullPath((Join-Path $projectRoot $PackageRoot))
$env:BIDMATE_EVAL_EXPORT_ROOT = [IO.Path]::GetFullPath((Join-Path $projectRoot $ExportRoot))
$env:BIDMATE_EVAL_REVIEW_PDF_ROOT = [IO.Path]::GetFullPath($PdfRoot)
& $python -m uvicorn bidmate_rag.eval_dataset.review.api:app --host 127.0.0.1 --port $Port
exit $LASTEXITCODE