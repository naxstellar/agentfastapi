Write-Host "Deleting the 'agentfastapi' namespace (removes all resources, secrets, and the PVC)..."
kubectl delete namespace agentfastapi --ignore-not-found
Write-Host "Done."
