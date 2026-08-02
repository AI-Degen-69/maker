# Ownership of fleet processes, shared by fleet-bg.ps1 and fleet-stop.ps1.
#
# WHY THIS EXISTS. Both scripts used to select processes with command-line
# wildcards -- "*strategy.fleet*" and friends -- and then Stop-Process -Force
# whatever matched. That pattern matches the same module name in ANY checkout
# and ANY session on the machine: a second clone, another user's fleet, a
# colleague's debugging run. Killing one takes its database writer with it, and
# nothing in the match says which fleet it belonged to.
#
# So ownership is RECORDED at launch instead of inferred at shutdown. The
# launcher writes the PIDs it actually started to run/fleet.pids.json, and
# shutdown stops those process trees and nothing else.
#
# PID reuse is the obvious hole in that plan -- Windows recycles PIDs freely,
# and a stale file naming PID 4820 must not kill whatever holds 4820 today. So
# each record carries the process start time, and a PID is only accepted when
# BOTH the id and its start time still match. A recycled PID has a different
# start time and is left alone.

$FleetPidFile = Join-Path $ProjectPath "run/fleet.pids.json"

# Reporting only -- never a kill list. Kept so an operator can be TOLD about a
# fleet this script does not own, rather than silently killing it.
$FleetPatterns = "*strategy.supervisor*", "*strategy.fleet*",
                 "*scripts.rerank_loop*", "*uvicorn*server.fleet_dash*"


function Save-FleetInstance {
    <#  Record the processes we just started, with their start times. #>
    param([Parameter(Mandatory)][hashtable]$Procs)   # name -> System.Diagnostics.Process

    $records = @()
    foreach ($name in $Procs.Keys) {
        $p = $Procs[$name]
        if ($null -eq $p) { continue }
        $records += [pscustomobject]@{
            name    = $name
            pid     = $p.Id
            # Round-trip format: parsed back exactly, and comparable across
            # culture settings.
            started = $p.StartTime.ToString("o")
        }
    }
    $payload = [pscustomobject]@{
        saved = (Get-Date).ToString("o")
        procs = $records
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $FleetPidFile) | Out-Null
    $payload | ConvertTo-Json -Depth 4 | Set-Content -Path $FleetPidFile -Encoding UTF8
}


function Get-FleetInstance {
    <#  The recorded processes that are STILL the ones we started.

        Returns records whose pid exists and whose start time matches. A pid
        that has been recycled, or has simply exited, is dropped. #>
    if (-not (Test-Path $FleetPidFile)) { return @() }
    try {
        $data = Get-Content $FleetPidFile -Raw | ConvertFrom-Json
    } catch {
        Write-Host "could not read $FleetPidFile ($($_.Exception.Message))" -ForegroundColor Yellow
        return @()
    }

    $live = @()
    foreach ($r in @($data.procs)) {
        try { $p = Get-Process -Id $r.pid -ErrorAction Stop } catch { continue }
        # The start-time check is what makes a recycled pid safe to ignore.
        if ($p.StartTime.ToString("o") -ne $r.started) {
            Write-Host "pid $($r.pid) was recycled; not touching it" -ForegroundColor DarkGray
            continue
        }
        $live += [pscustomobject]@{ name = $r.name; pid = $r.pid; proc = $p }
    }
    return $live
}


# THE FAILURE THIS CATCHES IS A NULL, NOT A NUMBER.
#
# A `$strays` local in fleet-stop.ps1 silently aliased its own `-Strays` switch
# (PowerShell variable names are case-insensitive). The assignment failed, the
# loop read `$p.ProcessId` off a SwitchParameter as 0, and the descendant walk
# started at System Idle -- so the script asked Windows to stop System, smss,
# Registry, Secure System and Memory Compression. Windows refused. That was
# luck; this is the guard.
#
# The threshold covers the reserved low ids (0 = System Idle, 4 = System). It
# does NOT enumerate every system process -- smss and Registry sit well above
# it. Those were only ever reachable BECAUSE the walk root was 0, and a walk
# root of 0 is now refused outright. Rejecting null/0/non-numeric is the load
# bearing part; the numeric floor is a backstop, not a whitelist of safe ids.
$FleetMinKillablePid = 100

function Assert-KillablePid {
    <#  Return the id as an int, or throw. Never returns something unkillable. #>
    param([Parameter(Mandatory)][AllowNull()][object]$ProcessId,
          [string]$Context = "")

    $id = 0
    if ($null -eq $ProcessId -or -not [int]::TryParse([string]$ProcessId, [ref]$id)) {
        throw "Refusing to stop a non-numeric process id '$ProcessId' $Context"
    }
    if ($id -lt $FleetMinKillablePid) {
        throw ("Refusing to stop PID $id $Context -- ids below " +
               "$FleetMinKillablePid are Windows system processes, so this is a " +
               "bug in the caller rather than a fleet process.")
    }
    return $id
}


function Get-DescendantPids {
    <#  Every process descending from $ParentId, deepest first.

        The supervisor spawns the fleet and the dashboard as children, so
        stopping only the recorded pid would orphan them -- and an orphaned
        fleet is a second writer on the same database. Walks the ParentProcessId
        graph rather than matching on command line. #>
    param([Parameter(Mandatory)][int]$ParentId)

    # Walking down from a system id enumerates half the machine as "children".
    $ParentId = Assert-KillablePid -ProcessId $ParentId -Context "(descendant walk root)"

    $all = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Select-Object ProcessId, ParentProcessId
    $out = @()
    $frontier = @($ParentId)
    while ($frontier.Count -gt 0) {
        $kids = $all | Where-Object { $frontier -contains $_.ParentProcessId } |
            Select-Object -ExpandProperty ProcessId
        $kids = @($kids | Where-Object { $out -notcontains $_ -and $_ -ne $ParentId })
        if ($kids.Count -eq 0) { break }
        $out += $kids
        $frontier = $kids
    }
    # Deepest first, so a parent cannot respawn a child we are about to stop.
    [array]::Reverse($out)
    return $out
}


function Stop-FleetTree {
    <#  Stop one recorded process and everything below it. #>
    param([Parameter(Mandatory)][AllowNull()][object]$ProcessId, [string]$Label = "")

    $target = Assert-KillablePid -ProcessId $ProcessId -Context $Label
    foreach ($child in Get-DescendantPids -ParentId $target) {
        # Checked individually too: the walk is only as trustworthy as the
        # ParentProcessId graph it reads.
        $safe = Assert-KillablePid -ProcessId $child -Context "(child of $target)"
        Write-Host "  stopping child PID $safe" -ForegroundColor DarkYellow
        Stop-Process -Id $safe -Force -ErrorAction SilentlyContinue
    }
    Write-Host "stopping PID $target $Label" -ForegroundColor Yellow
    Stop-Process -Id $target -Force -ErrorAction SilentlyContinue
}


function Stop-FleetInstance {
    <#  Stop the recorded fleet. Returns the number of trees stopped.

        The supervisor goes first: it restarts a child it owns, so killing the
        fleet while the supervisor lives just gets the fleet restarted. #>
    $live = @(Get-FleetInstance)
    if ($live.Count -eq 0) { return 0 }

    $ordered = @($live | Where-Object { $_.name -eq "supervisor" }) +
               @($live | Where-Object { $_.name -ne "supervisor" })
    foreach ($r in $ordered) {
        Stop-FleetTree -ProcessId $r.pid -Label "($($r.name))"
        Start-Sleep -Milliseconds 300
    }

    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline -and @(Get-FleetInstance).Count -gt 0) {
        Start-Sleep -Milliseconds 500
    }
    if (@(Get-FleetInstance).Count -gt 0) {
        throw "Fleet processes did not stop before the deadline."
    }
    Remove-Item $FleetPidFile -ErrorAction SilentlyContinue
    return $ordered.Count
}


function Find-FleetStrays {
    <#  Fleet-shaped processes this script does not own. REPORTING ONLY.

        These may belong to another checkout or another user, so they are
        described, never stopped. The operator decides. #>
    $ownedPids = @(Get-FleetInstance | ForEach-Object { $_.pid })
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $cl = $_.CommandLine
            $cl -and ($ownedPids -notcontains $_.ProcessId) -and
            ($FleetPatterns | Where-Object { $cl -like $_ })
        }
}
