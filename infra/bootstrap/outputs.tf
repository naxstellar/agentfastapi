output "project_ids" {
  value = local.project_ids
}

output "state_bucket" {
  value = google_storage_bucket.tfstate.name
}

output "image_repository" {
  value = "${var.region}-docker.pkg.dev/${google_project.shared.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "workload_identity_provider" {
  description = "Value for google-github-actions/auth `workload_identity_provider`."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "ci_service_accounts" {
  value = { for name, sa in google_service_account.ci : name => sa.email }
}

output "dns_name_servers" {
  description = "Set these at the registrar when the domain is not bought through Cloud Domains."
  value       = var.domain == "" ? [] : google_dns_managed_zone.main[0].name_servers
}
