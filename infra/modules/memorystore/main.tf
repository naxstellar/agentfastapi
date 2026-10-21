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

variable "private_services_connection" {
  type        = string
  description = "Output of the network module; the instance needs the private services peering."
}

variable "memory_size_gb" {
  type    = number
  default = 1
}

# BASIC has no replica: a maintenance restart drops sessions, queued Celery tasks and live streams. Acceptable
# for dev and staging; prod uses STANDARD_HA.
variable "tier" {
  type    = string
  default = "BASIC"
}

resource "google_redis_instance" "main" {
  project            = var.project
  name               = var.name
  region             = var.region
  tier               = var.tier
  memory_size_gb     = var.memory_size_gb
  redis_version      = "REDIS_7_0"
  authorized_network = var.network_id
  connect_mode       = "PRIVATE_SERVICE_ACCESS"
  auth_enabled       = true

  # Off for dev and staging; decide for prod.
  transit_encryption_mode = "DISABLED"

  depends_on = [var.private_services_connection]
}

output "host" {
  value = google_redis_instance.main.host
}

output "port" {
  value = google_redis_instance.main.port
}

# The app picks its own database numbers from this URL (broker DB 1, events DB 2).
output "redis_url" {
  value     = "redis://:${google_redis_instance.main.auth_string}@${google_redis_instance.main.host}:${google_redis_instance.main.port}/0"
  sensitive = true
}
