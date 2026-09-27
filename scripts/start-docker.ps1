$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Host "Starting Postgres + Redis + API, all in Docker (Ctrl+C to stop)..."
docker compose --profile full up --build
