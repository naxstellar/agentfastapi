<#
Adds Secret Manager versions for one environment from a gitignored env file (docs/gcp-setup.md, step 9).
Run it after Terraform (WP12) has created the secret *definitions*. Values are never printed or put on a command line.

  powershell -File scripts/gcp-set-secrets.ps1 -Env dev            # reads env/.env.dev
  powershell -File scripts/gcp-set-secrets.ps1 -Env dev -DryRun    # shows what it would do, calls nothing

env/.env.<Env> holds KEY=value lines for: AUTH0_CLIENT_ID, AUTH0_CLIENT_SECRET, SESSION_SECRET_KEY,
ANTHROPIC_API_KEY, OPENAI_API_KEY. (database-url and redis-url are written by Terraform, not by hand.)
A blank value is skipped. SESSION_SECRET_KEY is generated if the file has none and the secret has no version yet
(changing it later logs every user out).
#>
param(
    [Parameter(Mandatory)][ValidateSet('dev', 'staging')][string]$Env,
    [string]$Project = "agentfastapi-$Env-nv01",
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $ProjectRoot "env\.env.$Env"
if (-not (Test-Path $envFile)) { throw "Missing $envFile (see docs/gcp-setup.md, step 9)." }

# KEY in the file -> Secret Manager secret id (the names WP12 T12.8 creates)
$map = [ordered]@{
    AUTH0_CLIENT_ID     = 'auth0-client-id'
    AUTH0_CLIENT_SECRET = 'auth0-client-secret'
    SESSION_SECRET_KEY  = 'session-secret-key'
    ANTHROPIC_API_KEY   = 'anthropic-api-key'
    OPENAI_API_KEY      = 'openai-api-key'
}

$values = @{}
foreach ($line in Get-Content $envFile) {
    if ($line -match '^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$' -and $map.Contains($Matches[1])) {
        $values[$Matches[1]] = $Matches[2].Trim('"').Trim("'")
    }
}

function Test-HasVersion($secret) {
    $out = gcloud secrets versions list $secret --project $Project --limit 1 --format 'value(name)' 2>$null
    return ($LASTEXITCODE -eq 0 -and $out)
}

foreach ($key in $map.Keys) {
    $secret = $map[$key]
    $value = $values[$key]
    if (-not $value -and $key -eq 'SESSION_SECRET_KEY' -and -not $DryRun -and -not (Test-HasVersion $secret)) {
        $bytes = New-Object byte[] 48
        [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $value = [Convert]::ToBase64String($bytes)
        Write-Host "${secret}: generated a new session key"
    }
    if (-not $value) { Write-Host "${secret}: no value in $envFile, skipped"; continue }
    if ($DryRun) { Write-Host "${secret}: would add a version ($($value.Length) chars) to $Project"; continue }

    # A temp file, not a pipe: PowerShell appends a newline to piped text, and that would end up in the secret.
    $tmp = [IO.Path]::GetTempFileName()
    try {
        [IO.File]::WriteAllText($tmp, $value, (New-Object Text.UTF8Encoding($false)))
        gcloud secrets versions add $secret --project $Project --data-file $tmp | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not add a version to '$secret' (does Terraform's secrets module exist yet?)." }
        Write-Host "${secret}: new version added"
    } finally {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    }
}
Write-Host "Done. Pods pick up changes within the ExternalSecret refreshInterval (1h); restart them to apply sooner."
