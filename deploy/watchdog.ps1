# Restarts the backend if it has stopped answering, and leaves a record of every time it had to.
#
# Registered by deploy\install-watchdog.cmd to run every five minutes. Run it by hand to see
# what it would do:
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File E:\Fa-py\deploy\watchdog.ps1
#
# Why this exists. Nothing was watching this service, and on 2026-11-08 the PC rebooted and
# it stayed down for two days before anyone noticed - because a dead backend looks to a kiosk
# exactly like a rejected key, so the people standing at it blame the app and nobody tells
# anyone. An at-boot task alone would not have caught that: the process can also die, wedge,
# or lose the database while the machine stays up.
#
# Deliberately quiet on success. A line every five minutes saying "fine" would bury the three
# lines a year that matter, so only failures, restarts and recoveries are written.

$ErrorActionPreference = 'Stop'

$Root     = 'E:\Fa-py'
$Port     = 8443
$Health   = "https://127.0.0.1:$Port/v1/health"
$Launcher = Join-Path $Root 'deploy\start-server.cmd'
$LogDir   = Join-Path $Root 'data\logs'
$Log      = Join-Path $LogDir 'watchdog.log'

# Loading the face model takes about five seconds on this hardware, so a fresh start needs
# noticeably longer than a probe of a server that is already up. 90s is generous rather than
# tight: this runs unattended every five minutes and must never overlap itself.
$ProbeTimeoutSeconds   = 20
$StartupTimeoutSeconds = 90

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

function Write-Line([string] $Message) {
    $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    Add-Content -Path $Log -Value "$stamp  $Message" -Encoding utf8
}

function Get-HealthCode {
    # curl.exe rather than Invoke-WebRequest: -k skips the self-signed certificate without
    # having to disable validation process-wide, and it is already a dependency of the other
    # deploy scripts. A non-200, a timeout and a refused connection all end up as "down",
    # which is the only distinction this script acts on.
    $code = & curl.exe -sk --max-time $ProbeTimeoutSeconds -o NUL -w '%{http_code}' $Health 2>$null

    if ($LASTEXITCODE -ne 0) { return '0' }

    return "$code".Trim()
}

function Get-PortHolder {
    # Returns $null when the port is free, otherwise the process holding it and whether it is
    # OUR uvicorn. The caller must check IsOurs before stopping anything: this script runs
    # unattended as SYSTEM, so a wrong identification here is the one thing in it that could
    # do real damage.
    #
    # Identified by command line rather than by image path, which is the non-obvious part:
    # uv's .venv\Scripts\python.exe is a 45 KB trampoline that execs the real interpreter out
    # of %APPDATA%\uv\python\..., so the running process's Path points OUTSIDE $Root. An
    # earlier version checked the path and therefore refused to recognise its own server -
    # caught on 2026-11-10 by killing the service and watching the watchdog log "not touching
    # it". The command line carries both the app target and a certificate path under $Root,
    # neither of which another python on this box would have.
    $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
                  Select-Object -First 1

    if (-not $connection) { return $null }

    $heldBy  = $connection.OwningProcess
    $details = Get-CimInstance Win32_Process -Filter "ProcessId = $heldBy" -ErrorAction SilentlyContinue

    if (-not $details) {
        # Gone between the two calls, or not readable. Treat the port as occupied by somebody
        # else rather than guessing it is ours.
        return [pscustomobject]@{ Id = $heldBy; Name = 'unknown'; IsOurs = $false }
    }

    $line = "$($details.CommandLine)"
    $ours = ($line -like '*app.main:app*') -and ($line -like "*$Root*")

    return [pscustomobject]@{ Id = $heldBy; Name = $details.Name; IsOurs = $ours }
}

function Wait-ForHealth {
    $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)

    while ((Get-Date) -lt $deadline) {
        if ((Get-HealthCode) -eq '200') { return $true }
        Start-Sleep -Seconds 5
    }

    return $false
}

# --- the check itself -----------------------------------------------------------------------

$code = Get-HealthCode

if ($code -eq '200') { exit 0 }

$holder = Get-PortHolder

if ($holder -and -not $holder.IsOurs) {
    # Somebody else's process is on our port. Starting the backend now would fail to bind and
    # achieve nothing, and stopping theirs is not this script's business. Say so and stop -
    # this is a human's problem, and the log is how they find out it is theirs.
    Write-Line "health returned '$code' and $Port is held by pid $($holder.Id) ($($holder.Name)), which is not this deployment - leaving it alone and not starting a second copy. Attendance is DOWN until that process goes."
    exit 1
}

if ($holder) {
    # Ours, still holding the port, answering nothing. A restart is the only way out, and
    # starting another copy would just fail to bind.
    Write-Line "health returned '$code' but our pid $($holder.Id) still holds $Port - stopping it"

    try {
        Stop-Process -Id $holder.Id -Force
    } catch {
        Write-Line "could not stop pid $($holder.Id): $($_.Exception.Message)"
        exit 1
    }

    # Windows keeps the port briefly after the process goes, so a start attempted immediately
    # loses the bind. Measured while testing this script: about ten seconds is enough.
    Start-Sleep -Seconds 12
} else {
    Write-Line "health returned '$code' and nothing is listening on $Port - starting it"
}

if (-not (Test-Path $Launcher)) {
    Write-Line "cannot restart: $Launcher is missing"
    exit 1
}

Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', "`"$Launcher`"" -WindowStyle Hidden

if (Wait-ForHealth) {
    $now = Get-PortHolder
    Write-Line "back up as pid $($now.Id)"
    exit 0
}

# Said plainly, because this is the case a human has to look at: the restart did not work,
# and server.log is where the reason will be.
Write-Line "restart did not come up within ${StartupTimeoutSeconds}s - see data\logs\server.log"
exit 1
