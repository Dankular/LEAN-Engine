param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$tokenPath = Join-Path $projectRoot '.dev-api-token'
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (Test-Path -LiteralPath $venvPython) {
    $pythonExe = $venvPython
} else {
    $pythonCommand = Get-Command python -ErrorAction Stop
    $pythonExe = $pythonCommand.Source
}

if (-not $env:API_BEARER_TOKEN) {
    if (-not (Test-Path -LiteralPath $tokenPath)) {
        $generatedToken = & $pythonExe -c 'import secrets; print(secrets.token_urlsafe(32))'
        [System.IO.File]::WriteAllText($tokenPath, $generatedToken, [System.Text.Encoding]::UTF8)
    }
    $env:API_BEARER_TOKEN = (Get-Content -LiteralPath $tokenPath -Raw).Trim()
}

if (-not $env:KIMINA_BASE_URL) {
    $env:KIMINA_BASE_URL = 'http://127.0.0.1:8081/v1'
}
if (-not $env:KIMINA_MODEL) {
    $env:KIMINA_MODEL = 'Kimina-Prover-RL-0.6B.i1-Q6_K.gguf'
}

Write-Host "Starting the LEAN Engine API at http://127.0.0.1:$Port"
Write-Host "The local development token is stored in .dev-api-token (git-ignored)."

Push-Location $projectRoot
try {
    & $pythonExe -m uvicorn leanproof_service.api:app --app-dir src --host 127.0.0.1 --port $Port
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
