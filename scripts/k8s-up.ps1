$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Host "Building the api image (agentfastapi-api:local)..."
docker build -t agentfastapi-api:local .

# Docker Desktop's Kubernetes runs as an isolated (kind-based) node that does NOT share the
# classic Docker Engine's image store, even with "Use containerd for pulling and storing
# images" enabled -- a plain `docker build` is invisible to the cluster. This loads the image
# directly into the node's containerd, equivalent to `kind load docker-image` (kind CLI isn't
# assumed to be installed). Safe to re-run every time.
Write-Host "Loading the api image into the cluster node..."
$imageTar = Join-Path $env:TEMP "agentfastapi-api.tar"
docker save agentfastapi-api:local -o $imageTar

$nodeName = kubectl get nodes -o jsonpath='{.items[0].metadata.name}'
kubectl delete pod -n default -l "app.kubernetes.io/managed-by=kubectl-debug" --ignore-not-found *> $null
$debugOutput = kubectl debug "node/$nodeName" --image=busybox --profile=general -- sleep 300 2>&1
$debugPod = ($debugOutput | Select-String -Pattern 'Creating debugging pod (\S+)').Matches[0].Groups[1].Value
if (-not $debugPod) {
    Write-Error "Could not determine debug pod name from: $debugOutput"
    exit 1
}
kubectl wait --for=condition=Ready -n default "pod/$debugPod" --timeout=60s *> $null

# kubectl cp on Windows misparses an absolute path like "C:\Users\..." because it splits on
# ':' -- the drive letter's colon gets mistaken for a namespace/pod separator. Using a
# relative filename (via Push-Location into the tar's own directory) avoids this entirely.
Push-Location (Split-Path $imageTar -Parent)
kubectl cp (Split-Path $imageTar -Leaf) "default/${debugPod}:/host/tmp/agentfastapi-api.tar" -c debugger
$cpExitCode = $LASTEXITCODE
Pop-Location
if ($cpExitCode -ne 0) {
    Write-Error "kubectl cp of the image tar into the debug pod failed."
    exit 1
}

kubectl exec -n default $debugPod -c debugger -- chroot /host ctr -n k8s.io images import /tmp/agentfastapi-api.tar
if ($LASTEXITCODE -ne 0) {
    Write-Error "ctr images import failed."
    exit 1
}
kubectl delete pod -n default $debugPod --ignore-not-found *> $null
Remove-Item $imageTar -ErrorAction SilentlyContinue

Write-Host "Ensuring namespace 'agentfastapi' exists..."
kubectl get namespace agentfastapi *> $null
if (-not $?) {
    kubectl apply -f k8s/overlays/local/namespace.yaml
}

Write-Host "Ensuring secrets exist..."
kubectl get secret agentfastapi-secrets -n agentfastapi *> $null
if (-not $?) {
    kubectl create secret generic agentfastapi-secrets --from-env-file=env/.env -n agentfastapi
} else {
    Write-Host "Secret 'agentfastapi-secrets' already exists -- leaving it as-is. Delete it manually first if env/.env changed."
}

kubectl get secret agentfastapi-postgres -n agentfastapi *> $null
if (-not $?) {
    $envLines = Get-Content (Join-Path $ProjectRoot "env\.env")
    $pgUser = ($envLines | Select-String '^POSTGRES_USER=').ToString().Split('=', 2)[1]
    $pgPassword = ($envLines | Select-String '^POSTGRES_PASSWORD=').ToString().Split('=', 2)[1]
    $pgDb = ($envLines | Select-String '^POSTGRES_DB=').ToString().Split('=', 2)[1]
    $pgDatabaseUrl = "postgresql+psycopg2://${pgUser}:${pgPassword}@postgres:5432/${pgDb}"

    kubectl create secret generic agentfastapi-postgres -n agentfastapi `
        --from-literal=POSTGRES_USER=$pgUser `
        --from-literal=POSTGRES_PASSWORD=$pgPassword `
        --from-literal=POSTGRES_DB=$pgDb `
        --from-literal=DATABASE_URL=$pgDatabaseUrl
} else {
    Write-Host "Secret 'agentfastapi-postgres' already exists -- leaving it as-is."
}

kubectl get secret agentfastapi-tls -n agentfastapi *> $null
if (-not $?) {
    kubectl create secret tls agentfastapi-tls --cert=certs/localhost+2.pem --key=certs/localhost+2-key.pem -n agentfastapi
} else {
    Write-Host "Secret 'agentfastapi-tls' already exists -- leaving it as-is."
}

# Google ADC for the api (signed URLs) and worker (GCS, sandboxes): the laptop's file, copied into a secret that
# lives only in this cluster. It holds your refresh token, so it is created here and never committed. Optional.
$adc = Join-Path $env:APPDATA 'gcloud\application_default_credentials.json'
kubectl get secret agentfastapi-gcp-adc -n agentfastapi *> $null
if (-not $?) {
    if (Test-Path $adc) {
        kubectl create secret generic agentfastapi-gcp-adc -n agentfastapi --from-file=adc.json=$adc
    } else {
        Write-Warning "No ADC file at ${adc}: dataset processing and run_python won't work in the cluster (see CLAUDE.md 'GCS setup')."
    }
} else {
    Write-Host "Secret 'agentfastapi-gcp-adc' already exists -- leaving it as-is. Delete it manually first if your ADC changed."
}

Write-Host "Applying manifests..."
# Jobs are immutable, so the schema Job is replaced on every run; it retries until Postgres is up.
kubectl delete job migrate -n agentfastapi --ignore-not-found
kubectl apply -k k8s/overlays/local

# Re-running this script rebuilds and reloads the image under the same tag, but an unchanged
# Deployment spec won't make Kubernetes pick it up on its own -- force it.
Write-Host "Restarting the api, worker and beat deployments so they pick up the freshly loaded image..."
kubectl rollout restart deployment/api -n agentfastapi
kubectl rollout restart deployment/worker deployment/beat -n agentfastapi
kubectl wait --for=condition=complete job/migrate -n agentfastapi --timeout=180s
if (-not $?) {
    Write-Warning "The migrate Job did not complete: kubectl logs job/migrate -n agentfastapi"
}
kubectl rollout status deployment/api -n agentfastapi --timeout=120s
kubectl rollout status deployment/worker -n agentfastapi --timeout=180s
kubectl rollout status deployment/beat -n agentfastapi --timeout=120s

Write-Host "Done. Check status with: kubectl get pods -n agentfastapi"
Write-Host "Once the LoadBalancer is ready, the app is reachable at https://127.0.0.1:8000/"
