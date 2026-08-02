# Stop the fleet this checkout started.
#
# Hidden processes cannot be closed by shutting a window, which is the point of
# running them hidden and also the reason this script has to exist. There was
# previously no way to stop the fleet at all except by starting it again --
# `fleet-start.ps1` kills the old processes only on its way to launching new
# ones.
#
#   .\scripts\fleet-stop.ps1            stop the recorded fleet
#   .\scripts\fleet-stop.ps1 -Strays    also stop fleet-shaped processes this
#                                       checkout did not start (see below)
#
# The database is left exactly where it is. Stopping is not archiving.
#
# WHY -Strays IS OPT-IN. This script used to select processes by command-line
# wildcard -- "*strategy.fleet*" and friends -- and force-kill everything that
# matched. That matches the same module name in ANY checkout and ANY session on
# the machine, so running it could take down a colleague's fleet, or a second
# clone of your own, along with its database writer. Ownership is now recorded
# at launch in run/fleet.pids.json and shutdown is scoped to it.
#
# A fleet started before that file existed, or started by hand, is not recorded
# and will NOT be stopped by default -- it is reported instead. -Strays is how
# you say "yes, those are mine too", which is a decision this script is not
# entitled to make on your behalf.
param([switch]$Strays)

$ProjectPath = Split-Path $PSScriptRoot -Parent
. (Join-Path $PSScriptRoot "fleet-procs.ps1")

$stopped = Stop-FleetInstance
if ($stopped -gt 0) {
    Write-Host "fleet stopped ($stopped process tree(s))." -ForegroundColor Green
}

# NOT $strays. PowerShell variable names are case-insensitive, so `$strays`
# IS the `-Strays` switch parameter above. Assigning the result here wrote an
# object array into a SwitchParameter: the conversion failed, `$p.ProcessId`
# then read as 0, and the descendant walk started from System Idle -- so this
# tried to Stop-Process the Windows system processes. They are protected and
# survived, which is luck rather than safety.
$unowned = @(Find-FleetStrays)

if ($unowned.Count -eq 0) {
    if ($stopped -eq 0) { Write-Host "nothing was running." -ForegroundColor DarkGray }
    return
}

if (-not $Strays) {
    Write-Host ""
    Write-Host "$($unowned.Count) fleet-shaped process(es) NOT started by this checkout:" -ForegroundColor Yellow
    $unowned | ForEach-Object {
        $cl = ($_.CommandLine -replace '\s+', ' ')
        Write-Host "  PID $($_.ProcessId)  $($cl.Substring(0, [Math]::Min(90, $cl.Length)))" -ForegroundColor DarkGray
    }
    Write-Host "  Left running -- they may belong to another checkout or user." -ForegroundColor DarkGray
    Write-Host "  If they are yours: .\scripts\fleet-stop.ps1 -Strays" -ForegroundColor DarkGray
    return
}

# Explicitly authorised. Supervisors first, so a supervisor cannot restart a
# child we just stopped.
Write-Host ""
Write-Host "-Strays given: stopping $($unowned.Count) unowned process(es)" -ForegroundColor Yellow
$sups = @($unowned | Where-Object { $_.CommandLine -like "*strategy.supervisor*" })
$rest = @($unowned | Where-Object { $_.CommandLine -notlike "*strategy.supervisor*" })
foreach ($p in ($sups + $rest)) {
    Stop-FleetTree -ProcessId $p.ProcessId -Label "(stray)"
    Start-Sleep -Milliseconds 300
}

Start-Sleep -Seconds 2
$left = @(Find-FleetStrays)
if ($left.Count -gt 0) {
    Write-Host "still running: $(($left.ProcessId) -join ', ')" -ForegroundColor Red
} else {
    Write-Host "fleet stopped." -ForegroundColor Green
}
