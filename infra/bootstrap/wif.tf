# GitHub Actions authenticates with its OIDC token; no service account keys exist anywhere.
resource "google_iam_workload_identity_pool" "github" {
  project                   = google_project.shared.project_id
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"

  depends_on = [google_project_service.shared]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = google_project.shared.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"        = "assertion.sub"
    "attribute.repository"  = "assertion.repository"
    "attribute.environment" = "assertion.environment"
    "attribute.ref"         = "assertion.ref"
  }

  # Tokens from any other repository are rejected before any binding is checked.
  attribute_condition = "assertion.repository == \"${var.github_repository}\""

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

locals {
  wif_pool = google_iam_workload_identity_pool.github.name

  # service account -> who may impersonate it. A GitHub Environment name is only in the token when the job
  # declares `environment:`, so the staging identities sit behind the staging approval gate.
  ci_service_accounts = {
    "gha-build"          = { description = "Builds and pushes images", principal = "attribute.repository/${var.github_repository}" }
    "gha-deploy-dev"     = { description = "Deploys to the dev cluster", principal = "attribute.environment/dev" }
    "gha-deploy-staging" = { description = "Deploys to the staging cluster", principal = "attribute.environment/staging" }
    "gha-tf-dev"         = { description = "Terraform for dev from CI", principal = "attribute.environment/dev" }
    "gha-tf-staging"     = { description = "Terraform for staging from CI", principal = "attribute.environment/staging" }
  }
}

resource "google_service_account" "ci" {
  for_each = local.ci_service_accounts

  project      = google_project.shared.project_id
  account_id   = each.key
  display_name = each.key
  description  = each.value.description

  depends_on = [google_project_service.shared]
}

resource "google_service_account_iam_member" "ci_impersonation" {
  for_each = local.ci_service_accounts

  service_account_id = google_service_account.ci[each.key].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${local.wif_pool}/${each.value.principal}"
}

# Only the build identity writes images; clusters pull them with their own node identity.
resource "google_artifact_registry_repository_iam_member" "build_writer" {
  project    = google_project.shared.project_id
  location   = var.region
  repository = google_artifact_registry_repository.images.repository_id
  role       = "roles/artifactregistry.writer"
  member     = google_service_account.ci["gha-build"].member
}

# Terraform from CI keeps its state in the shared bucket.
resource "google_storage_bucket_iam_member" "tf_state" {
  for_each = toset(["gha-tf-dev", "gha-tf-staging"])

  bucket = google_storage_bucket.tfstate.name
  role   = "roles/storage.objectAdmin"
  member = google_service_account.ci[each.key].member
}
