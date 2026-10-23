# Register a Windows scheduled task that runs `chainscope sync` every 10 minutes.
# Run once from an elevated or normal PowerShell in the project root:
#   powershell -ExecutionPolicy Bypass -File python\tools\register_sync_task.ps1
# Remove it again with:
#   Unregister-ScheduledTask -TaskName "ChainScope BEM sync" -Confirm:$false
param(
    [string]$Job = "bem",
    [int]$EveryMinutes = 10,
    [int]$MaxMinutes = 9
)

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "找不到 $python；请先在项目根目录执行 uv sync"
}
$arguments = "-m chainscope sync --job $Job --max-minutes $MaxMinutes"
$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
# StartWhenAvailable: run a missed slot after sleep or reboot; IgnoreNew: never overlap.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes ($MaxMinutes + 5))
Register-ScheduledTask -TaskName "ChainScope $($Job.ToUpper()) sync" -Action $action `
    -Trigger $trigger -Settings $settings -Description "ChainScope 定时增量采集" -Force
Write-Output "已注册：每 $EveryMinutes 分钟运行 $python $arguments"
