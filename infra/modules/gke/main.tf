variable "project" {
  type = string
}

variable "region" {
  type = string
}

variable "name" {
  type = string
}

variable "network_id" {
  type = string
}

variable "subnetwork_id" {
  type = string
}

variable "node_service_account" {
  type        = string
  description = "Email of the node service account (created in infra/bootstrap)."
}

variable "deletion_protection" {
  type    = bool
  default = false
}

# Autopilot: Google manages nodes, so there is no node pool to size; billing is per pod request.
resource "google_container_cluster" "main" {
  project  = var.project
  name     = var.name
  location = var.region

  enable_autopilot    = true
  network             = var.network_id
  subnetwork          = var.subnetwork_id
  deletion_protection = var.deletion_protection

  release_channel {
    channel = "REGULAR"
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  # Nodes are private; the control plane keeps a public endpoint that IAM protects, because GitHub-hosted
  # runners have no fixed IPs to allow-list. The DNS endpoint is the one kubectl and CI should use.
  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
  }

  control_plane_endpoints_config {
    dns_endpoint_config {
      allow_external_traffic = true
    }
  }

  cluster_autoscaling {
    auto_provisioning_defaults {
      service_account = var.node_service_account
      oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    }
  }

  # 02:30 IST, when nobody is using a dev or staging environment.
  maintenance_policy {
    daily_maintenance_window {
      start_time = "21:00"
    }
  }
}

output "name" {
  value = google_container_cluster.main.name
}

output "location" {
  value = google_container_cluster.main.location
}

output "endpoint" {
  value = google_container_cluster.main.endpoint
}

output "ca_certificate" {
  value = google_container_cluster.main.master_auth[0].cluster_ca_certificate
}

# Workload Identity pool; bindings to Kubernetes service accounts need the cluster to exist first.
output "workload_pool" {
  value = "${var.project}.svc.id.goog"

  depends_on = [google_container_cluster.main]
}
