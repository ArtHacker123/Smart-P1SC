[CmdletBinding()]
param(
    [string]$ProjectPath = "",
    [string]$TaskName = "Smart-P1SC",
    [string]$PythonPath = "",
    [string]$ConfigPath = ""
)

$ErrorActionPreference = "Stop"

if (-not $ProjectPath) {
    $ProjectPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
}
$ProjectPath = (Resolve-Path -LiteralPath $ProjectPath).Path

if (-not $ConfigPath) {
    $ConfigPath = Join-Path $ProjectPath "config.json"
}
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Config not found: $ConfigPath"
}
$ConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path

if (-not $PythonPath) {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        throw "python is not on PATH; pass -PythonPath explicitly"
    }
    $PythonPath = $python.Source
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
$pythonw = Join-Path (Split-Path -Parent $PythonPath) "pythonw.exe"
if (Test-Path -LiteralPath $pythonw) {
    $PythonPath = $pythonw
}

$logDir = Join-Path $ProjectPath "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$arguments = "-m smart_p1_sc.daemon --config `"$ConfigPath`""
$action = New-ScheduledTaskAction -Execute $PythonPath -Argument $arguments -WorkingDirectory $ProjectPath
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Smart-P1SC print anomaly monitor" `
    -Force | Out-Null

Write-Output "Installed scheduled task '$TaskName' for $user."
Write-Output "Start it with: Start-ScheduledTask -TaskName '$TaskName'"
