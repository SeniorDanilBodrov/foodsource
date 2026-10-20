param(
    [ValidatePattern('^\d{2}:\d{2}$')]
    [string]$At = '06:00'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$scriptPath = Join-Path $PSScriptRoot 'run-collector.ps1'
$taskName = 'FoodSourceSupplierCollection'

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description 'FoodSource: обновляет разрешённые публичные источники поставщиков.' -Force | Out-Null
Write-Output "Задание '$taskName' создано: ежедневный сбор в $At."
