variable "region" {
  type    = string
  default = "asia-south1"
}

variable "suffix" {
  type    = string
  default = "nv01"
}

variable "billing_account" {
  type = string
}

variable "domain" {
  type        = string
  description = "Delegated domain served by the shared Cloud DNS zone (infra/bootstrap)."
  default     = "app.nakulvijay.com"
}

variable "dns_zone_name" {
  type    = string
  default = "agentfastapi"
}

variable "alert_emails" {
  type        = list(string)
  description = "Receive the budget alerts."
}

variable "budget_amount" {
  type        = number
  description = "Monthly budget for this project, in the billing account's currency (INR)."
  default     = 1000
}

variable "budget_currency" {
  type    = string
  default = "INR"
}

variable "local_dev_impersonators" {
  type        = list(string)
  description = "IAM members (user:you@example.com) allowed to impersonate local-dev from the laptop."
  default     = []
}

# Two-step bootstrap of the cluster add-ons: the first apply creates the cluster, the second installs the
# operator (install_addons), the third adds the ClusterSecretStore (create_cluster_secret_store) because its
# CRD only exists once the operator is running.
variable "install_addons" {
  type    = bool
  default = false
}

variable "create_cluster_secret_store" {
  type    = bool
  default = false
}

variable "deletion_protection" {
  type        = bool
  description = "Cluster and Cloud SQL deletion protection. Off in dev so the environment can be torn down."
  default     = false
}
