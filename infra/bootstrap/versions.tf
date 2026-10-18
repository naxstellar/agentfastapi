terraform {
  required_version = ">= 1.11"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # Created by this configuration (shared.tf); the first apply ran with local state, then it was migrated here.
  backend "gcs" {
    bucket = "agentfastapi-tfstate-nv01"
    prefix = "bootstrap"
  }
}

provider "google" {
  region = var.region
}
