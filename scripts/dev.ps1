param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$sourceRoot = Join-Path $projectRoot 'src'
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

Write-Host "Starting local API at http://127.0.0.1:$Port"
Write-Host "Watching Python source under $sourceRoot"
Write-Host "The local development token is stored in .dev-api-token (git-ignored)."

Push-Location $projectRoot
try {
    & $pythonExe -m uvicorn leanproof_service.api:app `
        --app-dir $sourceRoot `
        --host 127.0.0.1 `
        --port $Port `
        --reload `
        --reload-dir $sourceRoot
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
