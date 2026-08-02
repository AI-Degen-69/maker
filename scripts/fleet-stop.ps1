# Stop every fleet process.
#
# Hidden processes cannot be closed by shutting a window, which is the point of
# running them hidden and also the reason this script has to exist. There was
# previously no way to stop the fleet at all except by starting it again --
# `fleet-start.ps1` kills the old processes only on its way to launching new
# ones.
#
#   .\scripts\fleet-stop.ps1
#
# The database is left exactly where it is. Stopping is not archiving.

# The supervisor restarts a child it owns, so it must die first -- killing the
# fleet while the supervisor lives just gets the fleet restarted.
$ordered = @("*strategy.supervisor*", "*scripts.rerank_loop*",
             "*strategy.fleet*", "*uvicorn*server.fleet_dash*")

$found = $false
foreach ($pat in $ordered) {
    $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like $pat }
    foreach ($p in $procs) {
        $found = $true
        Write-Host "stopping PID $($p.ProcessId)  $pat" -ForegroundColor Yellow
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 400
}

Start-Sleep -Seconds 2
$left = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $cl = $_.CommandLine
                   $ordered | Where-Object { $cl -like $_ } }
if ($left) {
    Write-Host "still running: $($left.ProcessId -join ', ')" -ForegroundColor Red
} elseif ($found) {
    Write-Host "fleet stopped." -ForegroundColor Green
} else {
    Write-Host "nothing was running." -ForegroundColor DarkGray
}
