# Node identity for each environment's GKE Autopilot cluster. Created here, not in the env modules, because the
# image pull permission is on the shared project's repository and the env pipelines (gha-tf-*) must not need
# rights there.
locals {
  gke_node_projects = {
    dev     = google_project.dev.project_id
    staging = google_project.staging.project_id
  }
}

resource "google_service_account" "gke_nodes" {
  for_each = local.gke_node_projects

  project      = each.value
  account_id   = "gke-nodes"
  display_name = "GKE Autopilot nodes (${each.key})"

  depends_on = [google_project_service.dev, google_project_service.staging]
}

# Logs, metrics and the node agent's own needs.
resource "google_project_iam_member" "gke_nodes_default" {
  for_each = local.gke_node_projects

  project = each.value
  role    = "roles/container.defaultNodeServiceAccount"
  member  = "serviceAccount:${google_service_account.gke_nodes[each.key].email}"
}

# Pull app images from the shared repository.
resource "google_artifact_registry_repository_iam_member" "gke_nodes_pull" {
  for_each = local.gke_node_projects

  project    = google_artifact_registry_repository.images.project
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.gke_nodes[each.key].email}"
}
