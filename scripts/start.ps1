param(
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8000
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Host "Starting Postgres + Redis containers..."
docker compose up -d

Write-Host "Waiting for Postgres to be ready..."
$ready = $false
for ($i = 0; $i -lt 15; $i++) {
    docker compose exec -T postgres pg_isready -U agentfastapi *> $null
    if ($?) {
        $ready = $true
        break
    }
    Start-Sleep -Seconds 1
}
if (-not $ready) {
    Write-Warning "Postgres did not report ready in time; continuing anyway."
}

$uvicorn = Join-Path $ProjectRoot ".venv\Scripts\uvicorn.exe"
$certFile = Join-Path $ProjectRoot "certs\localhost+2.pem"
$keyFile = Join-Path $ProjectRoot "certs\localhost+2-key.pem"

Write-Host "Starting uvicorn on https://${HostName}:${Port}/ (Ctrl+C to stop; containers keep running)"
& $uvicorn app.main:app --host $HostName --port $Port --ssl-certfile $certFile --ssl-keyfile $keyFile --reload
