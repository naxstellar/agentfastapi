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
  description = "Output of the network module; the instance can only be created once the peering exists."
}

variable "tier" {
  type    = string
  default = "db-g1-small" # shared core, 1.7 GB, no SLA; move up (e.g. db-custom-1-3840) if connections or memory run short
}

variable "disk_size_gb" {
  type    = number
  default = 20
}

variable "point_in_time_recovery" {
  type    = bool
  default = false
}

variable "deletion_protection" {
  type    = bool
  default = false
}

variable "database_name" {
  type    = string
  default = "agentfastapi"
}

variable "user_name" {
  type    = string
  default = "agentfastapi"
}

resource "random_password" "db" {
  length  = 32
  special = false # the password goes into a URL
}

# Cloud SQL keeps a deleted instance's name reserved for about a week, so a destroy followed by an apply would
# fail on a fixed name. The suffix is stable for the life of the state and new after a destroy.
resource "random_id" "instance" {
  byte_length = 2
}

resource "google_sql_database_instance" "main" {
  project             = var.project
  name                = "${var.name}-${random_id.instance.hex}"
  region              = var.region
  database_version    = "POSTGRES_16"
  deletion_protection = var.deletion_protection

  settings {
    edition           = "ENTERPRISE"
    tier              = var.tier
    availability_type = "ZONAL"
    disk_type         = "PD_SSD"
    disk_size         = var.disk_size_gb
    disk_autoresize   = true

    ip_configuration {
      ipv4_enabled    = false # private IP only
      private_network = var.network_id
      ssl_mode        = "ENCRYPTED_ONLY"
    }

    backup_configuration {
      enabled                        = true
      start_time                     = "21:30" # UTC, 03:00 IST
      point_in_time_recovery_enabled = var.point_in_time_recovery
      backup_retention_settings {
        retained_backups = 7
      }
    }

    deletion_protection_enabled = var.deletion_protection
  }

  depends_on = [var.private_services_connection]
}

# ABANDON: when the instance is destroyed, dropping the role or database first can fail on owned objects.
resource "google_sql_database" "app" {
  project         = var.project
  instance        = google_sql_database_instance.main.name
  name            = var.database_name
  deletion_policy = "ABANDON"
}

resource "google_sql_user" "app" {
  project         = var.project
  instance        = google_sql_database_instance.main.name
  name            = var.user_name
  password        = random_password.db.result
  deletion_policy = "ABANDON"
}

output "instance_name" {
  value = google_sql_database_instance.main.name
}

output "private_ip" {
  value = google_sql_database_instance.main.private_ip_address
}

# Same driver the app uses (psycopg2); the password only ever reaches Secret Manager and Terraform state.
output "database_url" {
  value     = "postgresql+psycopg2://${google_sql_user.app.name}:${random_password.db.result}@${google_sql_database_instance.main.private_ip_address}:5432/${google_sql_database.app.name}?sslmode=require"
  sensitive = true
}
