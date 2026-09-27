param(
    [int]$Port = 8000
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

function Stop-ProcessTree {
    param([int]$RootProcessId)

    $allProcs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
    $toKill = New-Object System.Collections.Generic.Queue[int]
    $toKill.Enqueue($RootProcessId)
    $visited = New-Object System.Collections.Generic.HashSet[int]

    while ($toKill.Count -gt 0) {
        $pid_ = $toKill.Dequeue()
        if (-not $visited.Add($pid_)) { continue }

        Stop-Process -Id $pid_ -Force -ErrorAction SilentlyContinue

        $children = $allProcs | Where-Object { $_.ParentProcessId -eq $pid_ }
        foreach ($child in $children) {
            $toKill.Enqueue($child.ProcessId)
        }
    }

    return $visited
}

Write-Host "Checking for a host-run uvicorn process on port $Port..."
$connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
$killedAny = $false
foreach ($conn in $connections) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $($conn.OwningProcess)" -ErrorAction SilentlyContinue

    # Only kill python.exe (our host-run uvicorn). If port 8000 is instead published by the
    # 'full' Docker profile's api container, Windows reports Docker Desktop's own backend/proxy
    # process as the owner here -- killing that tree previously took down Docker Desktop itself.
    # docker compose down (below) safely releases container-published ports without touching
    # any host process, so anything that isn't python.exe is deliberately left alone.
    if ($proc -and $proc.Name -eq "python.exe") {
        Write-Host "Found host-run uvicorn (PID $($conn.OwningProcess)) -- stopping its process tree."
        $killed = Stop-ProcessTree -RootProcessId $conn.OwningProcess
        foreach ($pid_ in $killed) {
            Write-Host "Killed PID $pid_"
            $killedAny = $true
        }
    } elseif ($proc) {
        Write-Host "Port $Port is held by '$($proc.Name)' (PID $($conn.OwningProcess)), not python.exe -- leaving it alone (likely Docker's own port-proxy for the 'full' profile; docker compose down will release it safely)."
    }
}

if (-not $killedAny) {
    Write-Host "No host-run uvicorn process found listening on port $Port."
}

Write-Host "Stopping Postgres + Redis + api (if running) containers..."
# --profile full is passed unconditionally so `down` also tears down the api container when
# it was started via the full-Docker-profile workflow -- `docker compose down` alone ignores
# profiled services regardless of which mode was actually used to start them.
docker compose --profile full down

Write-Host "Done."
