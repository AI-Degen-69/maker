# Start the whole fleet with NO console windows.
#
# `fleet-start.ps1` runs the supervisor in the foreground so the operator can
# watch it, which ties the run to a PowerShell window and collects one terminal
# per process on the desktop. This starts the same processes detached and
# hidden, with every stream redirected to a file, so closing the window that
# launched it changes nothing.
#
#   .\scripts\fleet-bg.ps1              # keep the current sample
#   .\scripts\fleet-bg.ps1 -FreshRun    # archive the DB first
#
# Stop with .\scripts\fleet-stop.ps1. Watch with:
#   Get-Content logs\supervisor.log -Wait -Tail 20
param(
    [switch]$FreshRun
)

$ErrorActionPreference = "Stop"
$ProjectPath = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectPath
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectPath "logs") | Out-Null

# 1. Stop anything already running, children included. Stopping only the
# supervisor leaves the fleet and the dashboard alive, which produces a second
# writer on the same database and a port conflict on 8800.
$patterns = "*strategy.supervisor*", "*strategy.fleet*", "*scripts.rerank_loop*",
            "*uvicorn*server.fleet_dash*"
$running = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $cl = $_.CommandLine; $patterns | Where-Object { $cl -like $_ } }
if ($running) {
    $running | ForEach-Object {
        Write-Host "stopping PID $($_.ProcessId)" -ForegroundColor Yellow
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
    $deadline = (Get-Date).AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 500
        $left = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $cl = $_.CommandLine; $patterns | Where-Object { $cl -like $_ } }
    } while ($left -and (Get-Date) -lt $deadline)
    if ($left) { throw "Fleet processes did not stop before the deadline." }
}

# The dashboard cannot bind a port someone else owns, and a supervisor whose
# dashboard child dies on startup restarts it in a loop.
if (netstat -ano | Select-String ":8800\s+.*LISTENING") {
    throw "Port 8800 is occupied by an unrelated process; refusing to start."
}

# 2. A fresh sample archives rather than deletes -- a paper run that has been
# accumulating for hours is evidence, and evidence is not overwritten in place.
if ($FreshRun) {
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $archive = Join-Path $ProjectPath "run/archive/fleet_$stamp"
    New-Item -ItemType Directory -Force -Path $archive | Out-Null
    Get-ChildItem -Path (Join-Path $ProjectPath "run") -File -Filter "fleet.db*" -ErrorAction SilentlyContinue |
        Move-Item -Destination $archive -Force
    $state = Join-Path $ProjectPath "run/fleet_state.json"
    if (Test-Path $state) { Move-Item -Path $state -Destination $archive -Force }
    Write-Host "archived prior run to $archive" -ForegroundColor Yellow
}

$env:MAKER_DB = "run/fleet.db"

# 3. Start hidden. The supervisor owns the fleet and the dashboard as children,
# and children inherit the parent's hidden console -- so one hidden start
# yields three windowless processes, not one hidden and two visible.
#
# Streams are redirected because a hidden process still writes to stdout and
# that output would otherwise go nowhere. The supervisor's own logging already
# goes to logs/supervisor.log; these files catch what happens BEFORE logging is
# configured, which is exactly where a startup crash lands.
$sup = Start-Process -FilePath "python" `
    -ArgumentList "-m", "strategy.supervisor" `
    -WorkingDirectory $ProjectPath -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $ProjectPath "logs/supervisor.out.log") `
    -RedirectStandardError  (Join-Path $ProjectPath "logs/supervisor.err.log")

# The universe is short-dated by construction: without this, every market in
# run/markets.json resolves inside a day and the fleet quotes a dead file while
# reporting a perfectly healthy heartbeat.
$rr = Start-Process -FilePath "python" `
    -ArgumentList "-m", "scripts.rerank_loop" `
    -WorkingDirectory $ProjectPath -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $ProjectPath "logs/rerank.out.log") `
    -RedirectStandardError  (Join-Path $ProjectPath "logs/rerank.err.log")

Start-Sleep -Seconds 6

# A COUNT OF MATCHING PROCESSES IS NOT PROOF THESE TWO SURVIVED.
#
# $alive below counts anything matching the patterns, so a supervisor that died
# on a bad markets.json still printed a green success line while the fleet was
# not running. Ask the two Process objects we actually started, and name the
# log that holds the traceback -- the crash lands in the .err.log before
# logging is configured, which is the file nobody thinks to open.
$sup.Refresh()
$rr.Refresh()
$dead = @()
if ($sup.HasExited) { $dead += "supervisor (see logs\supervisor.err.log)" }
if ($rr.HasExited)  { $dead += "reranker (see logs\rerank.err.log)" }
if ($dead.Count -gt 0) {
    throw "Fleet startup failed: $($dead -join '; ')"
}

$alive = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $cl = $_.CommandLine; $patterns | Where-Object { $cl -like $_ } }
Write-Host ""
Write-Host "supervisor PID $($sup.Id) · rerank PID $($rr.Id) · $($alive.Count) processes up" -ForegroundColor Green
Write-Host "dashboard  http://127.0.0.1:8800"
Write-Host "logs       Get-Content logs\supervisor.log -Wait -Tail 20"
Write-Host "stop       .\scripts\fleet-stop.ps1"
