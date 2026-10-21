variable "project" {
  type = string
}

variable "region" {
  type = string
}

variable "name" {
  type        = string
  description = "Bucket name, e.g. agentfastapi-dev-data-nv01."
}

variable "cors_origins" {
  type        = list(string)
  description = "Browser origins allowed to PUT/GET through signed URLs."
}

resource "google_storage_bucket" "main" {
  project                     = var.project
  name                        = var.name
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false # never deletes a bucket that still holds user data

  # The browser uploads straight to GCS. x-goog-content-length-range is part of the signature, so the
  # preflight must allow it or every upload fails.
  cors {
    origin          = var.cors_origins
    method          = ["PUT", "GET", "HEAD"]
    response_header = ["Content-Type", "x-goog-content-length-range"]
    max_age_seconds = 3600
  }

  # Uploads that were never completed.
  lifecycle_rule {
    condition {
      age            = 1
      matches_prefix = ["pending/"]
    }
    action {
      type = "Delete"
    }
  }
}

output "name" {
  value = google_storage_bucket.main.name
}
