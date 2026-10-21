terraform {
  required_providers {
    google = {
      source                = "hashicorp/google"
      configuration_aliases = [google.billing]
    }
  }
}

variable "project" {
  type        = string
  description = "Environment project; the budget watches its spend."
}

variable "billing_account" {
  type = string
}

variable "display_name" {
  type = string
}

variable "amount" {
  type        = number
  description = "Monthly amount in the billing account's currency."
}

variable "currency_code" {
  type        = string
  description = "Must equal the billing account's currency (INR for 01677F-9D8614-53AC4B)."
}

variable "alert_emails" {
  type = list(string)
}

# The monitoring project owns the notification channels; the env project is fine.
resource "google_monitoring_notification_channel" "email" {
  for_each = toset(var.alert_emails)

  project      = var.project
  display_name = "Budget alert ${each.value}"
  type         = "email"

  labels = {
    email_address = each.value
  }
}

data "google_project" "this" {
  project_id = var.project
}

resource "google_billing_budget" "main" {
  provider = google.billing

  billing_account = var.billing_account
  display_name    = var.display_name

  budget_filter {
    projects = ["projects/${data.google_project.this.number}"]
  }

  amount {
    specified_amount {
      currency_code = var.currency_code
      units         = tostring(var.amount)
    }
  }

  threshold_rules {
    threshold_percent = 0.5
  }

  threshold_rules {
    threshold_percent = 0.9
  }

  threshold_rules {
    threshold_percent = 1.0
  }

  all_updates_rule {
    monitoring_notification_channels = [for c in google_monitoring_notification_channel.email : c.id]
    disable_default_iam_recipients   = false
  }
}
