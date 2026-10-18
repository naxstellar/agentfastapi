locals {
  shared_project  = "agentfastapi-shared-${var.suffix}"
  dev_project     = "agentfastapi-dev-${var.suffix}"
  staging_project = "agentfastapi-staging-${var.suffix}"

  project_ids = {
    shared  = local.shared_project
    dev     = local.dev_project
    staging = local.staging_project
  }

  # Environment projects run the app; the shared one holds state, images, DNS and CI identities.
  env_apis = [
    "aiplatform",
    "cloudquotas",
    "cloudresourcemanager",
    "cloudscheduler",
    "compute",
    "container",
    "iam",
    "iamcredentials",
    "logging",
    "monitoring",
    "redis",
    "secretmanager",
    "servicenetworking",
    "serviceusage",
    "sqladmin",
    "storage",
  ]

  shared_apis = [
    "artifactregistry",
    "billingbudgets",
    "cloudresourcemanager",
    "dns",
    "domains",
    "iam",
    "iamcredentials",
    "serviceusage",
    "storage",
    "sts",
  ]
}

resource "google_project" "shared" {
  project_id          = local.shared_project
  name                = local.shared_project
  billing_account     = var.billing_account
  auto_create_network = false
}

# Created by hand earlier (see EXTERNAL_RESOURCES.md); imported, not recreated.
import {
  to = google_project.dev
  id = "projects/${local.dev_project}"
}

resource "google_project" "dev" {
  project_id          = local.dev_project
  name                = local.dev_project
  billing_account     = var.billing_account
  auto_create_network = false
}

resource "google_project" "staging" {
  project_id          = local.staging_project
  name                = local.staging_project
  billing_account     = var.billing_account
  auto_create_network = false
}

resource "google_project_service" "shared" {
  for_each = toset(local.shared_apis)

  project                    = google_project.shared.project_id
  service                    = "${each.value}.googleapis.com"
  disable_on_destroy         = false
  disable_dependent_services = false
}

resource "google_project_service" "dev" {
  for_each = toset(local.env_apis)

  project                    = google_project.dev.project_id
  service                    = "${each.value}.googleapis.com"
  disable_on_destroy         = false
  disable_dependent_services = false
}

resource "google_project_service" "staging" {
  for_each = toset(local.env_apis)

  project                    = google_project.staging.project_id
  service                    = "${each.value}.googleapis.com"
  disable_on_destroy         = false
  disable_dependent_services = false
}
