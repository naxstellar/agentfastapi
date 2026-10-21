variable "project" {
  type = string
}

variable "name" {
  type        = string
  description = "Must match kubernetes.io/ingress.global-static-ip-name in the overlay, e.g. agentfastapi-dev-ip."
}

variable "dns_project" {
  type        = string
  description = "Project that holds the Cloud DNS zone (shared)."
}

variable "dns_zone" {
  type        = string
  description = "Cloud DNS managed zone name."
}

variable "hostname" {
  type        = string
  description = "Fully qualified name without the trailing dot, e.g. dev.app.nakulvijay.com."
}

# The Ingress and the Google-managed certificate attach to this address. An unused reserved address is billed, so
# there is no prevent_destroy: scripts/dev-down.ps1 releases it, and the next apply reserves a new one and
# repoints the DNS record (the managed certificate is issued again, which takes a while).
resource "google_compute_global_address" "ingress" {
  project = var.project
  name    = var.name
}

resource "google_dns_record_set" "ingress" {
  project      = var.dns_project
  managed_zone = var.dns_zone
  name         = "${var.hostname}."
  type         = "A"
  ttl          = 300
  rrdatas      = [google_compute_global_address.ingress.address]
}

output "address" {
  value = google_compute_global_address.ingress.address
}
