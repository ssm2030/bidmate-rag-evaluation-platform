param([int]$Port = 5678)

$ErrorActionPreference = 'Stop'
$env:N8N_HOST = '127.0.0.1'
$env:N8N_LISTEN_ADDRESS = '127.0.0.1'
$env:N8N_PORT = "$Port"
n8n start