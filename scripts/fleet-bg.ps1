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

. (Join-Path $PSScriptRoot "fleet-procs.ps1")

# 1. Stop the fleet WE recorded, children included. Stopping only the
# supervisor leaves the fleet and the dashboard alive, which produces a second
# writer on the same database and a port conflict on 8800.
#
# Scoped to the recorded instance rather than a command-line wildcard: the old
# pattern matched the same module name in any checkout or session on this
# machine, so starting a fleet here could kill someone else's -- and its
# database writer with it.
$stopped = Stop-FleetInstance
if ($stopped -gt 0) { Write-Host "stopped $stopped prior fleet process tree(s)" -ForegroundColor Yellow }

# Anything fleet-shaped we do NOT own is reported, never killed. It may be
# another checkout entirely; it may also be why port 8800 is busy, which the
# operator now gets told instead of having to guess.
$strays = @(Find-FleetStrays)
if ($strays.Count -gt 0) {
    Write-Host ""
    Write-Host "WARNING: $($strays.Count) fleet-shaped process(es) not started by this script:" -ForegroundColor Red
    # Collapse FIRST, then bound against the collapsed length. Bounding with
    # $_.CommandLine.Length while slicing the collapsed string throws
    # ArgumentOutOfRangeException the moment collapsing shortens it -- and this
    # scans arbitrary third-party processes, which is exactly where irregular
    # spacing comes from. Under $ErrorActionPreference = "Stop" that aborts
    # startup before the new fleet launches: a cosmetic line killing the run.
    $strays | ForEach-Object {
        $cl = ($_.CommandLine -replace '\s+', ' ')
        Write-Host "  PID $($_.ProcessId)  $($cl.Substring(0, [Math]::Min(90, $cl.Length)))" -ForegroundColor DarkGray
    }
    Write-Host "  Not stopping them -- they may belong to another checkout or user." -ForegroundColor DarkGray
    Write-Host "  If they are yours, stop them first: .\scripts\fleet-stop.ps1 -Strays" -ForegroundColor DarkGray
    Write-Host ""
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

# Recorded BEFORE the liveness check, so a process that dies during startup is
# still owned and can be cleaned up by fleet-stop rather than left orphaned.
Save-FleetInstance -Procs @{ supervisor = $sup; rerank = $rr }

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

# Our own processes plus the supervisor's children (fleet + dashboard), rather
# than every fleet-shaped process on the machine.
$alive = @(Get-FleetInstance).Count + @(Get-DescendantPids -ParentId $sup.Id).Count
Write-Host ""
Write-Host "supervisor PID $($sup.Id) · rerank PID $($rr.Id) · $alive processes up" -ForegroundColor Green
Write-Host "dashboard  http://127.0.0.1:8800"
Write-Host "logs       Get-Content logs\supervisor.log -Wait -Tail 20"
Write-Host "stop       .\scripts\fleet-stop.ps1"
