$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    throw 'Не найдено виртуальное окружение .venv. Сначала установите зависимости по README.'
}

Push-Location $projectRoot
try {
    & $python -m backend.collector
} finally {
    Pop-Location
}
