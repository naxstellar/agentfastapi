param(
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8000,
    [switch]$NoWorker   # API only: skip the Celery worker and beat
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Host "Starting Postgres + Redis containers..."
docker compose --env-file env/.env up -d

$pgUser = (Get-Content (Join-Path $ProjectRoot "env\.env") | Select-String '^POSTGRES_USER=').ToString().Split('=', 2)[1]

Write-Host "Waiting for Postgres to be ready..."
$ready = $false
for ($i = 0; $i -lt 15; $i++) {
    docker compose exec -T postgres pg_isready -U $pgUser *> $null
    if ($?) {
        $ready = $true
        break
    }
    Start-Sleep -Seconds 1
}
if (-not $ready) {
    Write-Warning "Postgres did not report ready in time; continuing anyway."
}

# `localhost` can resolve to ::1 where Windows' wslrelay answers instead of Docker (see CLAUDE.md caveats); shell env
# vars beat env/.env, so pin the host-run processes to IPv4.
foreach ($name in @("DATABASE_URL", "REDIS_URL")) {
    $line = Get-Content (Join-Path $ProjectRoot "env\.env") | Select-String "^$name=" | Select-Object -First 1
    if ($line) { Set-Item -Path "env:$name" -Value $line.ToString().Split('=', 2)[1].Replace('localhost', '127.0.0.1') }
}

# The Celery worker (agent runs, dataset processing) and beat (approval expiry) run in the background, logging to
# logs\*.log; their PIDs go to .run\*.pid so stop.ps1 can tree-kill them. Skipped when still running from before.
function Start-Background {
    param([string]$Name, [string[]]$Arguments)
    $pidFile = Join-Path $ProjectRoot ".run\$Name.pid"
    if (Test-Path $pidFile) {
        $old = [int](Get-Content $pidFile)
        if (Get-Process -Id $old -ErrorAction SilentlyContinue) {
            Write-Host "$Name already running (PID $old)."
            return
        }
    }
    New-Item -ItemType Directory -Force (Join-Path $ProjectRoot ".run"), (Join-Path $ProjectRoot "logs") | Out-Null
    $celery = Join-Path $ProjectRoot ".venv\Scripts\celery.exe"
    $proc = Start-Process -FilePath $celery -ArgumentList $Arguments -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $ProjectRoot "logs\$Name.log") `
        -RedirectStandardError (Join-Path $ProjectRoot "logs\$Name.err.log")
    Set-Content -Path $pidFile -Value $proc.Id
    Write-Host "Started $Name (PID $($proc.Id)); log: logs\$Name.log"
}

if (-not $NoWorker) {
    # --pool=threads is mandatory on Windows (no prefork).
    Start-Background -Name "worker" -Arguments @("-A", "app.workers.celery_app", "worker", "--pool=threads", "--concurrency=4", "--loglevel=INFO")
    Start-Background -Name "beat" -Arguments @("-A", "app.workers.celery_app", "beat", "--loglevel=INFO", "--schedule", ".run\celerybeat-schedule")
}

$uvicorn = Join-Path $ProjectRoot ".venv\Scripts\uvicorn.exe"
$certFile = Join-Path $ProjectRoot "certs\localhost+2.pem"
$keyFile = Join-Path $ProjectRoot "certs\localhost+2-key.pem"

Write-Host "Starting uvicorn on https://${HostName}:${Port}/ (Ctrl+C to stop; containers keep running)"
& $uvicorn app.main:app --host $HostName --port $Port --ssl-certfile $certFile --ssl-keyfile $keyFile --reload
