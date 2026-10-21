terraform {
  required_version = ">= 1.11"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.17"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.38"
    }
  }

  # Bucket created by infra/bootstrap.
  backend "gcs" {
    bucket = "agentfastapi-tfstate-nv01"
    prefix = "envs/dev"
  }
}

provider "google" {
  project = local.project
  region  = var.region

  # The Billing Budgets API wants a quota project; the shared project has it enabled.
  user_project_override = true
  billing_project       = local.shared_project
}

# The cluster does not exist on the first apply, so these read it only once install_addons is true.
data "google_client_config" "default" {}

data "google_container_cluster" "main" {
  count = var.install_addons ? 1 : 0

  project  = local.project
  name     = module.gke.name
  location = var.region
}

locals {
  cluster_host = try("https://${data.google_container_cluster.main[0].endpoint}", "")
  cluster_ca   = try(base64decode(data.google_container_cluster.main[0].master_auth[0].cluster_ca_certificate), "")
}

provider "kubernetes" {
  host                   = local.cluster_host
  token                  = data.google_client_config.default.access_token
  cluster_ca_certificate = local.cluster_ca
}

provider "helm" {
  kubernetes {
    host                   = local.cluster_host
    token                  = data.google_client_config.default.access_token
    cluster_ca_certificate = local.cluster_ca
  }
}
