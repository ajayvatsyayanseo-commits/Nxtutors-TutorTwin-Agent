##############################################################################
# Inputs.
#
# Defaults are the *staging* shape: small, cheap, scale to zero. Production
# overrides them in its own tfvars file rather than by editing this one, so a
# production value can never be the accident of a default nobody revisited.
##############################################################################

variable "project_id" {
  type        = string
  description = "GCP project id."
}

variable "project_number" {
  type        = string
  description = "GCP project number, for the budget filter."
  default     = ""
}

variable "region" {
  type        = string
  description = "Cloud Run and Cloud Tasks region. Keep it near Neon."
  default     = "asia-south1"
}

variable "environment" {
  type        = string
  description = "staging or production. Suffixes every resource name."

  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "Only staging and production are deployed. local and test run on a laptop."
  }
}

variable "api_image" {
  type        = string
  description = "Fully-qualified API image, digest-pinned in production."
}

variable "admin_image" {
  type        = string
  description = "Fully-qualified control-plane image."
}

# --- sizing -----------------------------------------------------------------
#
# 1 vCPU / 1Gi is measured, not guessed: PDF rasterisation for OCR is the peak,
# and it fits in ~700MB for the 40-page ceiling the media limits allow. Below
# 1Gi that page count OOMs; above it, the extra is idle.

variable "api_cpu" {
  type    = string
  default = "1"
}

variable "api_memory" {
  type    = string
  default = "1Gi"
}

variable "api_max_instances" {
  type        = number
  description = "A budget control as much as a scaling one: it bounds concurrent Postgres connections and concurrent provider spend."
  default     = 10
}

variable "admin_max_instances" {
  type    = number
  default = 3
}

variable "request_timeout_seconds" {
  type        = number
  description = "Must exceed the queue's dispatch deadline (540s) for job pushes."
  default     = 600
}

# --- queue ------------------------------------------------------------------

variable "queue_max_concurrent" {
  type        = number
  description = "The real ceiling on simultaneous OCR, vision spend and DB load."
  default     = 10
}

variable "queue_max_per_second" {
  type    = number
  default = 5
}

# --- secrets ----------------------------------------------------------------
#
# Names only. Values are written with `gcloud secrets versions add`, never by
# Terraform: state is stored, shared and diffed, and a value in state is a value
# in a bucket somebody can read.

variable "secret_names" {
  type = list(string)
  default = [
    "database-url",
    "database-migration-url",
    "internal-api-key",
    "anthropic-api-key",
    "openai-api-key",
    "r2-access-key-id",
    "r2-secret-access-key",
  ]
}

# --- non-secret configuration -----------------------------------------------

variable "api_env" {
  type        = map(string)
  description = "Plain environment for the API. Never put a credential here - it is visible in the service description to anyone with viewer."
  default     = {}
}

# --- budget -----------------------------------------------------------------

variable "billing_account" {
  type        = string
  description = "Leave empty to skip the budget alert."
  default     = ""
}

variable "monthly_budget" {
  type    = number
  default = 200
}

variable "budget_currency" {
  type    = string
  default = "USD"
}
