<#
Tears down everything in the dev environment that costs money, then lists what is left so you can check the bill
is zero. Run it in a PowerShell window of its own (Terraform needs the credentials set below).

  powershell -File scripts/dev-down.ps1          # asks before destroying
  powershell -File scripts/dev-down.ps1 -Yes     # no question
  powershell -File scripts/dev-down.ps1 -Verify  # only list what is still running, destroy nothing

Destroyed: GKE cluster, Cloud SQL (data and backups too), Memorystore, the VPC with its NAT and private peering,
the reserved ingress IP (billed while unused) and its DNS record.
Kept (free): service accounts and IAM, secret definitions, buckets, the budget, and the hand-made local-dev setup.
Not touched: the shared project (state bucket, images, Cloud DNS zone, ~0.20 USD a month for the zone).
Bring it back with `terraform apply` in infra/envs/dev. Secret Manager keeps its versions, so the five hand-loaded
secrets survive; database-url and redis-url are rewritten by that apply.
#>
param(
    [switch]$Yes,
    [switch]$Verify,
    [string]$Project = 'agentfastapi-dev-nv01'
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$env:CLOUDSDK_CONFIG = "$env:APPDATA\gcloud-terraform"
$env:GOOGLE_APPLICATION_CREDENTIALS = "$env:APPDATA\gcloud-terraform\application_default_credentials.json"

function Show-Remaining {
    Write-Host "`nStill present in $Project (each list should be empty):" -ForegroundColor Cyan
    $checks = [ordered]@{
        'GKE clusters'          = { gcloud container clusters list --project $Project --format 'value(name)' }
        'Cloud SQL instances'   = { gcloud sql instances list --project $Project --format 'value(name)' }
        'Memorystore instances' = { gcloud redis instances list --region asia-south1 --project $Project --format 'value(name)' }
        'Reserved addresses'    = { gcloud compute addresses list --project $Project --format 'value(name)' }
        'Forwarding rules'      = { gcloud compute forwarding-rules list --project $Project --format 'value(name)' }
        'Cloud NAT routers'     = { gcloud compute routers list --project $Project --format 'value(name)' }
        'VPC networks'          = { gcloud compute networks list --project $Project --format 'value(name)' }
        'Persistent disks'      = { gcloud compute disks list --project $Project --format 'value(name)' }
    }
    foreach ($name in $checks.Keys) {
        $found = & $checks[$name] 2>$null
        $state = if ($found) { ($found -join ', ') } else { 'none' }
        Write-Host ("  {0,-22} {1}" -f $name, $state)
    }
    Write-Host "Check the bill itself in Billing > Reports; charges can lag by a day." -ForegroundColor DarkGray
}

if ($Verify) { Show-Remaining; return }

if (-not $Yes) {
    $answer = Read-Host "Destroy the dev GKE cluster, Cloud SQL (all data), Memorystore, network and ingress IP in $Project? Type yes"
    if ($answer -ne 'yes') { Write-Host 'Cancelled.'; return }
}

# Load balancers that an Ingress created live outside Terraform. Delete them through Kubernetes first, or the VPC
# cannot be removed and they keep billing. Best effort: skipped when no cluster exists.
$cluster = gcloud container clusters list --project $Project --format 'value(name)' 2>$null
if ($cluster) {
    Write-Host "Removing Kubernetes load balancers from $cluster ..."
    gcloud container clusters get-credentials $cluster --region asia-south1 --project $Project --dns-endpoint 2>$null
    if ($LASTEXITCODE -eq 0) {
        kubectl delete ingress --all --all-namespaces --wait=true --timeout=300s 2>$null
        kubectl delete service --field-selector spec.type=LoadBalancer --all-namespaces --wait=true --timeout=300s 2>$null
    }
    else {
        Write-Warning 'Could not reach the cluster; any load balancer left behind must be deleted by hand.'
    }
}

Set-Location (Join-Path $ProjectRoot 'infra\envs\dev')

# The add-ons live inside the cluster that is about to disappear; forget them instead of asking a dead API server.
$state = terraform state list 2>$null
foreach ($addr in @('module.addons[0].helm_release.external_secrets', 'kubernetes_manifest.cluster_secret_store[0]')) {
    if ($state -contains $addr) { terraform state rm $addr | Out-Null }
}

$targets = 'module.gke', 'module.cloudsql', 'module.memorystore', 'module.network', 'module.ingress_dns'
$targetArgs = $targets | ForEach-Object { "-target=$_" }
terraform destroy -input=false -auto-approve @targetArgs
if ($LASTEXITCODE -ne 0) { throw "terraform destroy failed (exit $LASTEXITCODE). Run it again; do not delete anything by hand yet." }

Show-Remaining
