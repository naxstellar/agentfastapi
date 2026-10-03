param(
    [int]$Concurrency = 4
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

# --pool=threads: Windows can't use prefork, and the agent runs (WP4) need threads anyway.
# Needs Redis (docker compose) and, for dataset processing, GCS ADC (see CLAUDE.md "GCS setup").
$celery = Join-Path $ProjectRoot ".venv\Scripts\celery.exe"
Write-Host "Starting Celery worker (pool=threads, concurrency=$Concurrency; Ctrl+C to stop)"
& $celery -A app.workers.celery_app worker --pool=threads --concurrency=$Concurrency --loglevel=INFO
