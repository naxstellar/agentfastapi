# Terraform state for bootstrap and for every environment (prefixes: bootstrap, envs/dev, envs/staging).
resource "google_storage_bucket" "tfstate" {
  project                     = google_project.shared.project_id
  name                        = "agentfastapi-tfstate-${var.suffix}"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  # Keep history for rollback, but not forever.
  lifecycle_rule {
    condition {
      num_newer_versions = 20
      with_state         = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.shared]
}

resource "google_artifact_registry_repository" "images" {
  project       = google_project.shared.project_id
  location      = var.region
  repository_id = "agentfastapi"
  format        = "DOCKER"
  description   = "agentfastapi application images"

  # Untagged layers pile up with every build; tagged releases are kept.
  cleanup_policies {
    id     = "delete-untagged"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "604800s"
    }
  }

  depends_on = [google_project_service.shared]
}

# Public zone for the domain; Cloud Domains (or an outside registrar) points at its name servers.
resource "google_dns_managed_zone" "main" {
  count = var.domain == "" ? 0 : 1

  project     = google_project.shared.project_id
  name        = "agentfastapi"
  dns_name    = "${var.domain}."
  description = "Public zone for ${var.domain}"

  depends_on = [google_project_service.shared]
}
