param(
    [string]$PythonExecutable = "python"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvRoot = Join-Path $projectRoot ".venv-ai"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"
$modelRoot = Join-Path $projectRoot "models\rubert-tiny2"

if (-not (Test-Path -LiteralPath $venvPython)) {
    & $PythonExecutable -m venv $venvRoot
}

& $venvPython -m pip install -r (Join-Path $projectRoot "backend\requirements.local-ai.txt")
New-Item -ItemType Directory -Force -Path $modelRoot | Out-Null

$supportFiles = @{
    "tokenizer.json" = "https://huggingface.co/fissium/rubert-tiny2/resolve/main/tokenizer.json?download=true"
    "config.json" = "https://huggingface.co/fissium/rubert-tiny2/resolve/main/config.json?download=true"
}

foreach ($entry in $supportFiles.GetEnumerator()) {
    $target = Join-Path $modelRoot $entry.Key
    if (-not (Test-Path -LiteralPath $target)) {
        Write-Host "Downloading $($entry.Key)..."
        Invoke-WebRequest -Uri $entry.Value -OutFile $target
    }
}

$quantizedModel = Join-Path $modelRoot "model.int8.onnx"
if (-not (Test-Path -LiteralPath $quantizedModel)) {
    $sourceModel = Join-Path $modelRoot "model.onnx"
    if (-not (Test-Path -LiteralPath $sourceModel)) {
        Write-Host "Downloading model.onnx for one-time quantization..."
        Invoke-WebRequest -Uri "https://huggingface.co/fissium/rubert-tiny2/resolve/main/model.onnx?download=true" -OutFile $sourceModel
    }
    & $venvPython -c "from onnxruntime.quantization import quantize_dynamic, QuantType; quantize_dynamic(r'$sourceModel', r'$quantizedModel', weight_type=QuantType.QInt8)"
}

Push-Location $projectRoot
try {
    & $venvPython -c "from backend.semantic import semantic_engine; print(semantic_engine.status(load=True))"
}
finally {
    Pop-Location
}

Write-Host "Local AI search is ready. Run: .\.venv-ai\Scripts\python.exe -m backend.run"
