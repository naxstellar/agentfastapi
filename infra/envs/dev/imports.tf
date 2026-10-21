# Resources made by hand on 2026-10-03/04 for laptop development (EXTERNAL_RESOURCES.md). Terraform adopts them
# here instead of recreating them. Once applied these blocks are inert and can be deleted.
locals {
  local_dev_email = "local-dev@${local.project}.iam.gserviceaccount.com"
  local_dev_id    = "projects/${local.project}/serviceAccounts/${local.local_dev_email}"
}

import {
  to = module.local_bucket.google_storage_bucket.main
  id = "${local.project}/${local.local_bucket}"
}

import {
  to = module.iam.google_service_account.local_dev[0]
  id = local.local_dev_id
}

import {
  to = module.iam.google_storage_bucket_iam_member.local_dev_bucket[0]
  id = "${local.local_bucket} roles/storage.objectAdmin serviceAccount:${local.local_dev_email}"
}

import {
  to = module.iam.google_project_iam_member.local_dev_aiplatform[0]
  id = "${local.project} roles/aiplatform.user serviceAccount:${local.local_dev_email}"
}

import {
  to = module.iam.google_service_account_iam_member.local_dev_sign[0]
  id = "${local.local_dev_id} roles/iam.serviceAccountTokenCreator serviceAccount:${local.local_dev_email}"
}

# Your own impersonation grant (the first entry of local_dev_impersonators).
import {
  to = module.iam.google_service_account_iam_member.local_dev_impersonation["user:nakulvijay56@gmail.com"]
  id = "${local.local_dev_id} roles/iam.serviceAccountTokenCreator user:nakulvijay56@gmail.com"
}
