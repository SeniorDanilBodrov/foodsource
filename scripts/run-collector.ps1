$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv-ai\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    throw 'Не найдено виртуальное окружение .venv-ai. Сначала установите зависимости локального AI по README.'
}

Push-Location $projectRoot
try {
    & $python -m backend.collector
} finally {
    Pop-Location
}
