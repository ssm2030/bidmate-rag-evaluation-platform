param([int]$Port = 8121)

$ErrorActionPreference = 'Stop'
$env:PYTHONPATH = 'src'
$env:BIDMATE_EVAL_ENABLE_MOCK_MODEL = 'true'
uv run uvicorn bidmate_rag.eval_dataset.automation.api:app --host 127.0.0.1 --port $Port