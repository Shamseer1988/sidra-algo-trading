[CmdletBinding()]
param(
    [string]$Time = "08:30AM",
    [switch]$Uninstall = $false
)

$ErrorActionPreference = 'Stop'
$TaskName = "IntradaySentinel-UpstoxDailyAuth"
$WorkingDir = (Get-Location).Path
$ScriptPath = Join-Path $WorkingDir "scripts\upstox-daily-auth.ps1"

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Successfully removed scheduled task: $TaskName" -ForegroundColor Green
    } else {
        Write-Host "Task $TaskName was not found." -ForegroundColor Yellow
    }
    exit 0
}

Write-Host "Configuring Windows Scheduled Task for Upstox Daily Auto-Authentication..." -ForegroundColor Cyan

$argString = "-ExecutionPolicy Bypass -NoProfile -File `"" + $ScriptPath + "`" -Docker"

$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argString -WorkingDirectory $WorkingDir

$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At (Get-Date $Time)

$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "Automated Daily Upstox OAuth TOTP token renewal and Telegram alert for Intraday Sentinel trading system." -Force | Out-Null

Write-Host ""
Write-Host "Scheduled Task Created Successfully!" -ForegroundColor Green
Write-Host ("• Task Name : " + $TaskName) -ForegroundColor White
Write-Host ("• Schedule  : Every Monday to Friday at " + $Time + " IST") -ForegroundColor White
Write-Host ("• Action    : " + $ScriptPath + " -Docker") -ForegroundColor White
Write-Host "• Telegram  : Renewal alert dispatched upon every run" -ForegroundColor White
Write-Host ""
Write-Host "To view task: Get-ScheduledTask -TaskName '$TaskName'" -ForegroundColor Cyan
Write-Host "To remove   : .\scripts\setup-scheduled-auth.ps1 -Uninstall" -ForegroundColor Cyan
