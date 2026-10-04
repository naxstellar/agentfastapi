$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

# Celery beat schedules the periodic tasks (today: agent.expire_approvals, every 60 s). Run one instance next to
# the worker(s); without it, a code approval nobody answers keeps its run `awaiting_approval` indefinitely.
$celery = Join-Path $ProjectRoot ".venv\Scripts\celery.exe"
Write-Host "Starting Celery beat (Ctrl+C to stop)"
& $celery -A app.workers.celery_app beat --loglevel=INFO
