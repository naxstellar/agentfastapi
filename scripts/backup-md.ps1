# Zips every *.md file in the repo (they are gitignored, so git does not hold them) into a backup folder
# outside the repo. Env files with secrets are NOT included.
#   powershell -File scripts/backup-md.ps1 [-Dest <dir>] [-Keep <n>]
# As a Claude Code hook (-FromHook) it reads the tool-call JSON on stdin and does nothing unless the edited file is a .md.
param(
    [string]$Dest = (Join-Path $env:USERPROFILE 'Backups\agentfastapi-md'),
    [int]$Keep = 14,
    [switch]$FromHook
)

$ErrorActionPreference = 'Stop'

if ($FromHook) {
    $raw = [Console]::In.ReadToEnd()
    try { $path = ($raw | ConvertFrom-Json).tool_input.file_path } catch { exit 0 }
    if (-not $path -or $path -notmatch '\.md$') { exit 0 }
}

$repo = Split-Path -Parent $PSScriptRoot
$skip = '\\(\.git|\.venv|node_modules|\.pytest_cache|\.terraform|logs|\.run)\\'
$files = Get-ChildItem -Path $repo -Recurse -File -Filter *.md | Where-Object { $_.FullName -notmatch $skip }

New-Item -ItemType Directory -Force $Dest | Out-Null
$stage = Join-Path ([IO.Path]::GetTempPath()) ("agentfastapi-md-" + [guid]::NewGuid().ToString('N'))
try {
    foreach ($f in $files) {
        $rel = $f.FullName.Substring($repo.Length).TrimStart('\')
        $target = Join-Path $stage $rel
        New-Item -ItemType Directory -Force (Split-Path $target) | Out-Null
        Copy-Item $f.FullName $target
    }
    $latest = Join-Path $Dest 'agentfastapi-md-latest.zip'
    $daily = Join-Path $Dest ("agentfastapi-md-{0:yyyyMMdd}.zip" -f (Get-Date))
    Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $latest -Force
    Copy-Item $latest $daily -Force
    Get-ChildItem $Dest -Filter 'agentfastapi-md-2*.zip' | Sort-Object Name -Descending | Select-Object -Skip $Keep | Remove-Item -Force
    Write-Host "Backed up $($files.Count) md files to $latest"
}
finally {
    Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
}
