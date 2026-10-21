variable "project" {
  type = string
}

# Definitions only. The values never pass through Terraform, git or CI logs: scripts/gcp-set-secrets.ps1 adds
# the five hand-managed ones, and the env root writes database-url and redis-url from the resources it creates.
variable "secret_ids" {
  type = list(string)
  default = [
    "auth0-client-id",
    "auth0-client-secret",
    "session-secret-key",
    "anthropic-api-key",
    "openai-api-key",
    "database-url",
    "redis-url",
  ]
}

resource "google_secret_manager_secret" "main" {
  for_each = toset(var.secret_ids)

  project   = var.project
  secret_id = each.value

  replication {
    auto {}
  }
}

output "secret_ids" {
  value = { for id, s in google_secret_manager_secret.main : id => s.id }
}
