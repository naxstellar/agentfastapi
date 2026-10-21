variable "project" {
  type = string
}

variable "workload_pool" {
  type        = string
  description = "<project>.svc.id.goog, from the gke module (the pool only exists once the cluster does)."
}

variable "namespace" {
  type    = string
  default = "agentfastapi"
}

variable "bucket_name" {
  type        = string
  description = "The environment's data bucket."
}

# local-dev: the identity the laptop impersonates (docs/gcp-setup.md). Only the dev environment sets this.
variable "create_local_dev" {
  type    = bool
  default = false
}

variable "local_bucket_name" {
  type    = string
  default = ""
}

variable "local_dev_impersonators" {
  type        = list(string)
  default     = []
  description = "IAM members (user:you@example.com) that may impersonate local-dev."
}

locals {
  workloads = toset(["api", "worker"])

  # Roles every app identity gets on the project.
  app_project_roles = [
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/aiplatform.user", # Agent Engine sandboxes: the worker runs code, the API deletes sandboxes with a conversation
  ]

  app_role_bindings = {
    for pair in setproduct(local.workloads, local.app_project_roles) :
    "${pair[0]}/${pair[1]}" => { workload = pair[0], role = pair[1] }
  }
}

resource "google_service_account" "app" {
  for_each = local.workloads

  project      = var.project
  account_id   = "agentfastapi-${each.value}" # account ids need at least 6 characters; the Kubernetes service accounts stay api / worker
  display_name = "agentfastapi ${each.value}"
}

resource "google_project_iam_member" "app" {
  for_each = local.app_role_bindings

  project = var.project
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.app[each.value.workload].email}"
}

resource "google_storage_bucket_iam_member" "app_data" {
  for_each = local.workloads

  bucket = var.bucket_name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.app[each.value].email}"
}

# Signed URLs: the account signs through IAM signBlob, so it needs to be able to create tokens for itself.
resource "google_service_account_iam_member" "app_sign" {
  for_each = local.workloads

  service_account_id = google_service_account.app[each.value].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.app[each.value].email}"
}

# Pods run as the Kubernetes service accounts api / worker (k8s/base/serviceaccounts.yaml).
resource "google_service_account_iam_member" "app_workload_identity" {
  for_each = local.workloads

  service_account_id = google_service_account.app[each.value].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.workload_pool}[${var.namespace}/${each.value}]"
}

# External Secrets Operator reads Secret Manager as this account.
resource "google_service_account" "external_secrets" {
  project      = var.project
  account_id   = "external-secrets"
  display_name = "External Secrets Operator"
}

resource "google_project_iam_member" "external_secrets" {
  project = var.project
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.external_secrets.email}"
}

resource "google_service_account_iam_member" "external_secrets_workload_identity" {
  service_account_id = google_service_account.external_secrets.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.workload_pool}[external-secrets/external-secrets]"
}

# ---- local-dev (created by hand on 2026-10-04; imported, see EXTERNAL_RESOURCES.md) ----
resource "google_service_account" "local_dev" {
  count = var.create_local_dev ? 1 : 0

  project      = var.project
  account_id   = "local-dev"
  display_name = "Local dev (signed URLs)"
}

resource "google_storage_bucket_iam_member" "local_dev_bucket" {
  count = var.create_local_dev ? 1 : 0

  bucket = var.local_bucket_name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.local_dev[0].email}"
}

resource "google_project_iam_member" "local_dev_aiplatform" {
  count = var.create_local_dev ? 1 : 0

  project = var.project
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.local_dev[0].email}"
}

resource "google_service_account_iam_member" "local_dev_sign" {
  count = var.create_local_dev ? 1 : 0

  service_account_id = google_service_account.local_dev[0].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.local_dev[0].email}"
}

resource "google_service_account_iam_member" "local_dev_impersonation" {
  for_each = var.create_local_dev ? toset(var.local_dev_impersonators) : toset([])

  service_account_id = google_service_account.local_dev[0].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = each.value
}

output "app_service_accounts" {
  value = { for name, sa in google_service_account.app : name => sa.email }
}

output "external_secrets_service_account" {
  value = google_service_account.external_secrets.email
}
