variable "project" {
  type = string
}

variable "region" {
  type = string
}

variable "name" {
  type        = string
  description = "Prefix for the network resources, e.g. agentfastapi-dev."
}

variable "subnet_cidr" {
  type    = string
  default = "10.10.0.0/20"
}

variable "pods_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

variable "services_cidr" {
  type    = string
  default = "10.30.0.0/20"
}

# Every environment has its own project and VPC, and the VPCs are never peered, so the ranges can repeat.
resource "google_compute_network" "main" {
  project                 = var.project
  name                    = var.name
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "main" {
  project                  = var.project
  name                     = var.name
  region                   = var.region
  network                  = google_compute_network.main.id
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true # GCS, Vertex AI / Agent Engine and Secret Manager without leaving Google's network

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = var.pods_cidr
  }

  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = var.services_cidr
  }
}

# Private Service Access: Cloud SQL and Memorystore get private IPs from this range.
resource "google_compute_global_address" "private_services" {
  project       = var.project
  name          = "${var.name}-private-services"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = google_compute_network.main.id
}

resource "google_service_networking_connection" "private_services" {
  network                 = google_compute_network.main.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_services.name]

  # Right after Cloud SQL / Memorystore are deleted, Google still reports the connection as in use and refuses to
  # remove it. ABANDON leaves it to disappear with the VPC (it is free), so a teardown does not fail on it.
  deletion_policy = "ABANDON"
}

# Private nodes have no public IPs, so Auth0 and the LLM APIs are reached through NAT.
resource "google_compute_router" "main" {
  project = var.project
  name    = var.name
  region  = var.region
  network = google_compute_network.main.id
}

resource "google_compute_router_nat" "main" {
  project                            = var.project
  name                               = var.name
  region                             = var.region
  router                             = google_compute_router.main.name
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

output "network_id" {
  value = google_compute_network.main.id
}

output "network_name" {
  value = google_compute_network.main.name
}

output "subnetwork_id" {
  value = google_compute_subnetwork.main.id
}

# Consumers that need the peering to exist (Cloud SQL, Memorystore) depend on this value.
output "private_services_connection" {
  value = google_service_networking_connection.private_services.id
}
