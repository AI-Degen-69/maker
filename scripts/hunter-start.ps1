# SPREAD HUNTER START SCRIPT
# Starts the Spread Hunter strategy stack in the background with windowless execution.
#
# Usage:
#   .\scripts\hunter-start.ps1              # keep the current sample
#   .\scripts\hunter-start.ps1 -FreshRun    # archive prior DB and start clean sample
param(
    [switch]$FreshRun
)

$ErrorActionPreference = "Stop"
$ProjectPath = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectPath
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectPath "logs") | Out-Null

. (Join-Path $PSScriptRoot "theme-loader.ps1")
. (Join-Path $PSScriptRoot "hunter-procs.ps1")

$modeSubtitle = if ($FreshRun) { "Two-Sided Maker Strategy | Mode: -FreshRun (Archive prior DB)" } else { "Two-Sided Maker Strategy | Mode: Standard Run (Preserve DB)" }

Write-ProfileBanner -Title "SPREAD HUNTER - STACK LAUNCHER" `
                    -Subtitle $modeSubtitle `
                    -Style "Info"
Write-Host ""

Write-ProfileInfo -Message "[1/5] Checking prior instances and port availability..." -Detail "(FreshRun = $FreshRun)"

# 1. Stop any previously recorded Spread Hunter instance (including child processes)
$stopped = Stop-HunterInstance
if ($stopped -gt 0) { 
    Write-ProfileWarning -Message "Stopped prior instance:" -Detail "$stopped Spread Hunter process tree(s)" 
}

# 2. Check for unowned stray processes (excluding bankroll bots)
$strays = @(Find-HunterStrays)
if ($strays.Count -gt 0) {
    Write-ProfileWarning -Message "Stray Processes:" -Detail "Found $($strays.Count) unowned hunter process(es)."
}

# 3. Check for port 8800 conflict and auto-kill strays that hold it
$portOccupied = netstat -ano | Select-String ":8800\s+.*LISTENING"
if ($portOccupied) {
    if ($strays.Count -gt 0) {
        Write-ProfileInfo -Message "Port 8800 occupied by strays. Auto-killing..."
        foreach ($p in $strays) {
            Stop-HunterTree -ProcessId $p.ProcessId -Label "(stray)"
            Start-Sleep -Milliseconds 300
        }
        Start-Sleep -Seconds 2
        $portOccupied = netstat -ano | Select-String ":8800\s+.*LISTENING"
        if ($portOccupied) {
            Write-ProfileError -Message "Port 8800 Conflict:" -Detail "Port 8800 is still occupied after killing strays."
            throw "Port 8800 occupied."
        }
        Write-ProfileSuccess -Message "Strays killed. Port 8800 freed."
    } else {
        Write-ProfileError -Message "Port 8800 Conflict:" -Detail "Port 8800 is occupied by a non-hunter process."
        throw "Port 8800 occupied."
    }
} elseif ($strays.Count -gt 0) {
    Write-ProfileInfo -Message "Killing strays anyway..."
    foreach ($p in $strays) {
        Stop-HunterTree -ProcessId $p.ProcessId -Label "(stray)"
    }
}

# 4. Archive prior database if -FreshRun requested
if ($FreshRun) {
    Write-ProfileInfo -Message "[2/5] Archiving prior run data (-FreshRun active)..."
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $archive = Join-Path $ProjectPath "run/archive/hunter_$stamp"
    New-Item -ItemType Directory -Force -Path $archive | Out-Null
    Get-ChildItem -Path (Join-Path $ProjectPath "run") -File -Filter "fleet.db*" -ErrorAction SilentlyContinue |
        Move-Item -Destination $archive -Force
    $state = Join-Path $ProjectPath "run/fleet_state.json"
    if (Test-Path $state) { Move-Item -Path $state -Destination $archive -Force }
    Write-ProfileSuccess -Message "Archived prior database to:" -Detail $archive
} else {
    Write-ProfileInfo -Message "[2/5] Preserving active database:" -Detail "run/fleet.db"
}

# Environment setup
$env:HUNTER_DB = "run/fleet.db"
$env:SPREAD_HUNTER_DB = "run/fleet.db"
$env:HUNTER_DEPTH_TRIAL_USD = "500"
$env:HUNTER_VOLUME_TRIAL_USD = "125000"
$env:HUNTER_MARGINAL_FLOOR = "0.0001"

# 5. Start supervisor in hidden background process
$sup = Start-Process -FilePath "python" `
    -ArgumentList "-m", "strategy.supervisor" `
    -WorkingDirectory $ProjectPath -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $ProjectPath "logs/supervisor.out.log") `
    -RedirectStandardError  (Join-Path $ProjectPath "logs/supervisor.err.log")

Save-HunterInstance -Procs @{ supervisor = $sup }

Write-ProfileInfo -Message "[3/5] Supervisor launched (PID $($sup.Id)). Spawning engine & dashboards..."
Write-ProfileInfo -Message "[4/5] Waiting for FastAPI servers (ports 8800/8801) and workers to boot..."

for ($i = 6; $i -gt 0; $i--) {
    Start-Sleep -Seconds 1
}

# 6. Verify supervisor is alive
$sup.Refresh()
if ($sup.HasExited) {
    Write-ProfileError -Message "Spread Hunter startup failed:" -Detail "Supervisor exited immediately (check logs/supervisor.err.log)"
    throw "Supervisor exited."
}

$alive = @(Get-HunterInstance).Count + @(Get-DescendantPids -ParentId $sup.Id).Count

Write-Host ""
Write-ProfileSuccess -Message "All systems operational!" -Detail "Supervisor PID $($sup.Id) | $alive processes active"
Write-ProfileKeyValue -Key "Main Dashboard" -Value "http://127.0.0.1:8800" -Style "Link" -KeyWidth 20
Write-ProfileKeyValue -Key "Market Scan" -Value "http://127.0.0.1:8801/?view=scan" -Style "Link" -KeyWidth 20
Write-ProfileKeyValue -Key "Live Logs" -Value "Get-Content logs\supervisor.err.log -Wait -Tail 20" -Style "Command" -KeyWidth 20
Write-ProfileKeyValue -Key "Stop Command" -Value "hunter-stop" -Style "Highlight" -KeyWidth 20
Write-Host ""

try {
    Start-Process "http://127.0.0.1:8800"
    Write-ProfileSuccess -Message "Opened http://127.0.0.1:8800 in default browser."
} catch {
    Write-ProfileWarning -Message "Could not open browser automatically:" -Detail "Navigate to http://127.0.0.1:8800"
}
Write-Host ""
