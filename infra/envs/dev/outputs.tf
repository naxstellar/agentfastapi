output "project" {
  value = local.project
}

output "cluster" {
  value = {
    name     = module.gke.name
    location = module.gke.location
  }
}

output "get_credentials" {
  value = "gcloud container clusters get-credentials ${module.gke.name} --region ${var.region} --project ${local.project} --dns-endpoint"
}

output "data_bucket" {
  value = module.data_bucket.name
}

output "app_service_accounts" {
  value = module.iam.app_service_accounts
}

output "ingress_ip" {
  value = module.ingress_dns.address
}

output "hostname" {
  value = local.hostname
}

output "cloudsql_private_ip" {
  value = module.cloudsql.private_ip
}

output "redis_host" {
  value = module.memorystore.host
}
