<#
    Install the Polymarket weather bot as a background task on Windows.

        powershell -ExecutionPolicy Bypass -File deploy\install-windows.ps1

    Idempotent: safe to re-run to upgrade an existing install.

    This registers a Scheduled Task rather than a true Windows service. A real
    service needs a wrapper (NSSM, WinSW) to supervise a non-service binary,
    and buys only the ability to run with nobody logged in -- which a desktop
    that sleeps will not deliver anyway. Task Scheduler is built in.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "PolyWeatherBot"
)

$ErrorActionPreference = "Stop"

$AppDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
# pythonw.exe is python.exe without a console window. The bot logs to
# logs\bot.log, and the file handler works with no console streams at all, so
# nothing is lost by running windowless.
$PyW = Join-Path $AppDir ".venv\Scripts\pythonw.exe"
$MainPy = Join-Path $AppDir "main.py"

Write-Host "==> Checking the virtualenv"
if (-not (Test-Path $PyW)) {
    Write-Host "    no .venv found at $AppDir\.venv -- creating one"
    $sys = Get-Command python -ErrorAction SilentlyContinue
    if (-not $sys) { throw "Python is not on PATH. Install Python 3.10+ first." }
    & $sys.Source -m venv (Join-Path $AppDir ".venv")
}
$Py = Join-Path $AppDir ".venv\Scripts\python.exe"
& $Py -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if ($LASTEXITCODE -ne 0) { throw "This bot needs Python 3.10 or newer." }

Write-Host "==> Installing dependencies"
& $Py -m pip install -q --upgrade pip
& $Py -m pip install -q -r (Join-Path $AppDir "requirements.txt")

foreach ($d in @("data", "logs")) {
    $p = Join-Path $AppDir $d
    if (-not (Test-Path $p)) { New-Item -ItemType Directory -Path $p | Out-Null }
}

$EnvFile = Join-Path $AppDir ".env"
if (-not (Test-Path $EnvFile)) {
    Copy-Item (Join-Path $AppDir ".env.example") $EnvFile
    Write-Host "    created .env from the example -- EDIT IT BEFORE STARTING"
}

Write-Host "==> Registering the scheduled task '$TaskName'"

$action = New-ScheduledTaskAction -Execute $PyW -Argument "`"$MainPy`"" -WorkingDirectory $AppDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# ExecutionTimeLimit 0 means "never time out". The default is three days, after
# which Task Scheduler would kill a perfectly healthy bot.
# MultipleInstances IgnoreNew is the crash-loop / double-run guard: two copies
# would mirror every leader fill twice and collide on Telegram's long poll.
# The battery settings matter on a laptop -- by default Windows refuses to
# start a task on battery and stops one when you unplug.
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal | Out-Null

Write-Host @"

Done.

  1. Edit the config:      notepad $EnvFile
     (at minimum TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

  2. Sanity-check a poll:  & "$Py" "$MainPy" --scan

  3. Start it now:         Start-ScheduledTask -TaskName $TaskName
     Check it is running:  Get-ScheduledTask -TaskName $TaskName
     Follow the logs:      Get-Content "$AppDir\logs\bot.log" -Wait -Tail 20
     Stop it:              Stop-ScheduledTask -TaskName $TaskName
     Remove it:            Unregister-ScheduledTask -TaskName $TaskName

The task starts at logon. It runs windowless, so the log file is the only place
you will see output. A sleeping PC stops the bot -- see the README.

The bot starts in PAPER mode and will not place a real order until you both set
TRADING_MODE=live and send /arm in Telegram.
"@
