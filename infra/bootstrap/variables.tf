variable "region" {
  type    = string
  default = "asia-south1"
}

variable "suffix" {
  type        = string
  description = "Random suffix that makes the project ids globally unique."
  default     = "nv01"
}

variable "billing_account" {
  type        = string
  description = "Billing account id linked to all three projects."
}

variable "github_repository" {
  type        = string
  description = "owner/name; only workflows of this repository can use Workload Identity Federation."
  default     = "naxstellar/agentfastapi"
}

variable "domain" {
  type        = string
  description = "Registered domain (no trailing dot). Empty until it is bought; the Cloud DNS zone is created once it is set."
  default     = ""
}
