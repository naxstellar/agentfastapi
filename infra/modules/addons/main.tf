variable "external_secrets_service_account" {
  type        = string
  description = "Google service account email External Secrets Operator uses through Workload Identity."
}

variable "chart_version" {
  type    = string
  default = "0.20.4"
}

# ESO is the only in-cluster dependency of the app: it turns Secret Manager entries into the Kubernetes Secret
# agentfastapi-secrets that the pods read with envFrom (k8s/overlays/*/externalsecret.yaml).
resource "helm_release" "external_secrets" {
  name             = "external-secrets"
  namespace        = "external-secrets"
  create_namespace = true
  repository       = "https://charts.external-secrets.io"
  chart            = "external-secrets"
  version          = var.chart_version
  atomic           = true
  timeout          = 600

  set {
    name  = "installCRDs"
    value = "true"
  }

  set {
    name  = "serviceAccount.name"
    value = "external-secrets"
  }

  set {
    name  = "serviceAccount.annotations.iam\\.gke\\.io/gcp-service-account"
    value = var.external_secrets_service_account
  }
}
