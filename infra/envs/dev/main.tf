locals {
  env            = "dev"
  project        = "agentfastapi-${local.env}-${var.suffix}"
  shared_project = "agentfastapi-shared-${var.suffix}"
  prefix         = "agentfastapi-${local.env}"
  hostname       = "${local.env}.${var.domain}"

  data_bucket  = "agentfastapi-${local.env}-data-${var.suffix}"
  local_bucket = "agentfastapi-local-${var.suffix}" # laptop development, made by hand on 2026-10-04
}

module "network" {
  source = "../../modules/network"

  project = local.project
  region  = var.region
  name    = local.prefix
}

module "gke" {
  source = "../../modules/gke"

  project              = local.project
  region               = var.region
  name                 = local.prefix
  network_id           = module.network.network_id
  subnetwork_id        = module.network.subnetwork_id
  node_service_account = "gke-nodes@${local.project}.iam.gserviceaccount.com" # infra/bootstrap
  deletion_protection  = var.deletion_protection
}

module "cloudsql" {
  source = "../../modules/cloudsql"

  project                     = local.project
  region                      = var.region
  name                        = local.prefix
  network_id                  = module.network.network_id
  private_services_connection = module.network.private_services_connection
  deletion_protection         = var.deletion_protection
}

module "memorystore" {
  source = "../../modules/memorystore"

  project                     = local.project
  region                      = var.region
  name                        = local.prefix
  network_id                  = module.network.network_id
  private_services_connection = module.network.private_services_connection
}

module "data_bucket" {
  source = "../../modules/storage"

  project      = local.project
  region       = var.region
  name         = local.data_bucket
  cors_origins = ["https://${local.hostname}"]
}

module "local_bucket" {
  source = "../../modules/storage"

  project      = local.project
  region       = var.region
  name         = local.local_bucket
  cors_origins = ["https://127.0.0.1:8000"]
}

module "iam" {
  source = "../../modules/iam"

  project                 = local.project
  workload_pool           = module.gke.workload_pool
  bucket_name             = module.data_bucket.name
  create_local_dev        = true
  local_bucket_name       = module.local_bucket.name
  local_dev_impersonators = var.local_dev_impersonators
}

module "secrets" {
  source = "../../modules/secrets"

  project = local.project
}

# Terraform owns these two values because it creates the resources they point at. The other five secrets are
# loaded by scripts/gcp-set-secrets.ps1. The values land in this state, which is why the state bucket is locked down.
resource "google_secret_manager_secret_version" "database_url" {
  secret      = module.secrets.secret_ids["database-url"]
  secret_data = module.cloudsql.database_url
}

resource "google_secret_manager_secret_version" "redis_url" {
  secret      = module.secrets.secret_ids["redis-url"]
  secret_data = module.memorystore.redis_url
}

module "ingress_dns" {
  source = "../../modules/ingress-dns"

  project     = local.project
  name        = "${local.prefix}-ip"
  dns_project = local.shared_project
  dns_zone    = var.dns_zone_name
  hostname    = local.hostname
}

module "budget" {
  source = "../../modules/budget"

  project         = local.project
  billing_account = var.billing_account
  display_name    = "${local.prefix} monthly"
  amount          = var.budget_amount
  currency_code   = var.budget_currency
  alert_emails    = var.alert_emails
}

module "addons" {
  count  = var.install_addons ? 1 : 0
  source = "../../modules/addons"

  external_secrets_service_account = module.iam.external_secrets_service_account
}

# Lets every ExternalSecret read Secret Manager in this project as the external-secrets Google service account.
resource "kubernetes_manifest" "cluster_secret_store" {
  count = var.create_cluster_secret_store ? 1 : 0

  manifest = {
    apiVersion = "external-secrets.io/v1beta1"
    kind       = "ClusterSecretStore"
    metadata = {
      name = "gcp-secret-manager"
    }
    spec = {
      provider = {
        gcpsm = {
          projectID = local.project
          auth = {
            workloadIdentity = {
              clusterLocation  = var.region
              clusterName      = module.gke.name
              clusterProjectID = local.project
              serviceAccountRef = {
                name      = "external-secrets"
                namespace = "external-secrets"
              }
            }
          }
        }
      }
    }
  }

  depends_on = [module.addons]
}
