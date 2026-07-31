param(
    [switch]$FreshRun
)

# ==========================================
# Polymarket Fleet Startup & Supervisor Script
# ==========================================

# Resolve from this script so the launcher works from any checkout path.
$ProjectPath = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$DashboardUrl = "http://127.0.0.1:8800"

Set-Location $ProjectPath

Write-Host "[1/4] Checking for existing supervisor processes..." -ForegroundColor Cyan

# 1. Detect and stop the whole prior fleet, using native Windows process
# metadata rather than Get-Process.CommandLine (which is not populated on all
# PowerShell hosts). Stopping only the supervisor leaves its children alive and
# can create a second writer or a port conflict during a fresh run.
$RunningFleet = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -like "*strategy.supervisor*" -or
        $_.CommandLine -like "*strategy.fleet*" -or
        $_.CommandLine -like "*uvicorn*server.fleet_dash*" -or
        $_.CommandLine -like "*uvicorn*8800*"
    }

if ($RunningFleet) {
    foreach ($proc in $RunningFleet) {
        Write-Host "[ACTION] Found prior fleet process (PID: $($proc.ProcessId)). Terminating..." -ForegroundColor Yellow
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
    # Do not archive or restart while a child is still alive. The supervisor
    # owns children, but force-stopping it can leave them briefly visible.
    $deadline = (Get-Date).AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 500
        $remaining = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.CommandLine -like "*strategy.supervisor*" -or
                $_.CommandLine -like "*strategy.fleet*" -or
                $_.CommandLine -like "*uvicorn*server.fleet_dash*" -or
                $_.CommandLine -like "*uvicorn*8800*"
            }
    } while ($remaining -and (Get-Date) -lt $deadline)
    if ($remaining) {
        throw "Fleet processes did not stop before the restart deadline."
    }
    do {
        Start-Sleep -Milliseconds 500
        $listener = netstat -ano | Select-String ":8800\s+.*LISTENING"
    } while ($listener -and (Get-Date) -lt $deadline)
    if ($listener) {
        throw "Port 8800 is still occupied; refusing to archive or restart."
    }
    Write-Host "[OK] Existing fleet processes terminated and port 8800 is free." -ForegroundColor Green
} else {
    Write-Host "[OK] No conflicting fleet processes found." -ForegroundColor Green
}

# Even without a matching fleet process, another service may own 8800. Never
# archive a sample and start a supervisor that cannot bind its dashboard port.
$listener = netstat -ano | Select-String ":8800\s+.*LISTENING"
if ($listener) {
    throw "Port 8800 is occupied by an unrelated process; refusing to restart."
}

# 2. A changed bankroll/config invalidates the old sample. Preserve the old
# database and all SQLite sidecars before creating the clean run requested by
# the operator. Without the state file move, the dashboard can show zombie
# markets while the new DB is still empty.
if ($FreshRun) {
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $archive = Join-Path $ProjectPath "run/archive/fleet_$stamp"
    New-Item -ItemType Directory -Force -Path $archive | Out-Null
    Get-ChildItem -Path (Join-Path $ProjectPath "run") -File -Filter "fleet.db*" -ErrorAction SilentlyContinue |
        Move-Item -Destination $archive -Force
    $state = Join-Path $ProjectPath "run/fleet_state.json"
    if (Test-Path $state) {
        Move-Item -Path $state -Destination $archive -Force
    }
    Write-Host "`n[2/5] Archived prior paper run to $archive" -ForegroundColor Yellow
} else {
    Write-Host "`n[2/5] Preserving current database (use -FreshRun for a clean sample)..." -ForegroundColor Cyan
}

# 3. Set the database used by both child processes.
Write-Host "`n[3/5] Setting environment variables..." -ForegroundColor Cyan
$env:MAKER_DB = "run/fleet.db"

# 4. Open the dashboard in the default browser after three seconds.
Write-Host "`n[4/5] Launching Dashboard in default browser..." -ForegroundColor Cyan
Start-Job -ScriptBlock {
    Start-Sleep -Seconds 3
    Start-Process "http://127.0.0.1:8800"
} | Out-Null

# 5. Run the supervisor in the foreground so its child processes are owned.
Write-Host "`n[5/5] Starting strategy.supervisor..." -ForegroundColor Green
Write-Host "=========================================================" -ForegroundColor Gray
python -m strategy.supervisor