##############################################################################
# TutorTwin - Google Cloud infrastructure.
#
# Small on purpose. This provisions the things that are *hard to reproduce by
# hand and easy to get wrong*: identities, IAM bindings, the queue's retry
# policy, and the wiring between them. It does not try to own the two managed
# services that live outside Google - Neon and Cloudflare R2 - because their
# providers would add two more credentials, two more state dependencies and two
# more failure modes to a `terraform apply`, in exchange for creating one
# database and one bucket. Those are documented in DEPLOYMENT.md instead.
#
# What this file refuses to do, deliberately:
#   * no VPC connector, no NAT gateway - every dependency is a public TLS
#     endpoint, and a VPC would add a fixed hourly cost to a design whose whole
#     point is scaling to zero
#   * no min_instance_count above 0 - an idle instance is a bill for nothing
#   * no secret *values* - only references to Secret Manager
##############################################################################

terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  api_name   = "tutortwin-api-${var.environment}"
  admin_name = "tutortwin-admin-${var.environment}"
}

##############################################################################
# Artifact Registry
##############################################################################

resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = "tutortwin"
  format        = "DOCKER"
  description   = "TutorTwin API and control plane images"

  # Keep the last N of each image. Without this the registry grows forever and
  # the storage bill is the one nobody ever looks at.
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }
}

##############################################################################
# Service accounts
#
# Three identities, because they need three different sets of permission:
#   api      - runs the API, reads secrets, writes to the queue
#   admin    - runs the control plane, reads only its own secret
#   invoker  - what Cloud Tasks and Cloud Scheduler present when calling us
#
# One shared account would mean the control plane could enqueue jobs and the
# queue could read the database password.
##############################################################################

resource "google_service_account" "api" {
  account_id   = "tutortwin-api-${var.environment}"
  display_name = "TutorTwin API (${var.environment})"
}

resource "google_service_account" "admin" {
  account_id   = "tutortwin-admin-${var.environment}"
  display_name = "TutorTwin control plane (${var.environment})"
}

resource "google_service_account" "invoker" {
  account_id   = "tutortwin-invoker-${var.environment}"
  display_name = "Cloud Tasks / Scheduler OIDC identity (${var.environment})"
}

##############################################################################
# Secrets
#
# Created here, *populated by hand or by a deploy pipeline*. Terraform never
# holds a secret value: state is stored, shared and diffed, and a value in state
# is a value in a bucket somebody can read.
##############################################################################

resource "google_secret_manager_secret" "app" {
  for_each  = toset(var.secret_names)
  secret_id = "${each.value}-${var.environment}"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_iam_member" "api_reads" {
  for_each  = google_secret_manager_secret.app
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

##############################################################################
# Cloud Run - API
#
# Request-based billing (the default for gen2 with cpu_idle), min 0, and a
# max_instance_count that is a *budget control* as much as a scaling knob: it
# bounds concurrent Postgres connections and concurrent provider spend at the
# same time.
##############################################################################

resource "google_cloud_run_v2_service" "api" {
  name     = local.api_name
  location = var.region

  # Public ingress: Cloud Tasks pushes over the internet, and Lead Intake will
  # call /v1/events from outside GCP in Phase 08. Every route is authenticated -
  # OIDC for the queue, shared secret for the bridge, sessions for the admin API.
  ingress = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.api.email

    scaling {
      min_instance_count = 0
      max_instance_count = var.api_max_instances
    }

    # 80 concurrent requests per instance. The work is IO-bound - waiting on a
    # provider or on Neon - so a low concurrency would start instances to sit
    # idle. The Postgres pool is 2+2 per container, which is what actually
    # bounds database load.
    max_instance_request_concurrency = 80
    timeout                          = "${var.request_timeout_seconds}s"

    containers {
      image = var.api_image

      resources {
        limits = {
          cpu    = var.api_cpu
          memory = var.api_memory
        }
        # CPU only while a request is in flight: this is what makes the billing
        # request-based rather than instance-based.
        cpu_idle          = true
        startup_cpu_boost = true
      }

      ports {
        container_port = 8080
      }

      dynamic "env" {
        for_each = var.api_env
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = google_secret_manager_secret.app
        content {
          name = upper(replace("TUTORTWIN_${env.key}", "-", "_"))
          value_source {
            secret_key_ref {
              secret  = env.value.secret_id
              version = "latest"
            }
          }
        }
      }

      # Readiness gates traffic on the database being reachable, so an instance
      # that cannot serve is never sent a request.
      startup_probe {
        http_get {
          path = "/readyz"
        }
        initial_delay_seconds = 2
        timeout_seconds       = 3
        period_seconds        = 3
        failure_threshold     = 10
      }

      # Liveness touches nothing: a database blip must not get healthy
      # containers killed and restarted into the same blip.
      liveness_probe {
        http_get {
          path = "/healthz"
        }
        period_seconds    = 30
        timeout_seconds   = 3
        failure_threshold = 3
      }
    }
  }

  depends_on = [google_secret_manager_secret_iam_member.api_reads]
}

##############################################################################
# Cloud Run - control plane
#
# A separate service. Fewer instances, smaller box, same scale-to-zero: it is
# used by a handful of operators, not by a queue.
##############################################################################

resource "google_cloud_run_v2_service" "admin" {
  name     = local.admin_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.admin.email

    scaling {
      min_instance_count = 0
      max_instance_count = var.admin_max_instances
    }

    max_instance_request_concurrency = 40
    timeout                          = "60s"

    containers {
      image = var.admin_image

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      ports {
        container_port = 8080
      }

      env {
        name  = "TUTORTWIN_API_URL"
        value = google_cloud_run_v2_service.api.uri
      }

      env {
        name  = "TUTORTWIN_ENVIRONMENT"
        value = var.environment
      }
    }
  }
}

##############################################################################
# Cloud Tasks
#
# `max_concurrent_dispatches` is the real ceiling on how many media jobs can hit
# Postgres and the providers at once - a far more important number than
# max_instances, because each job is expensive.
#
# Retries are bounded here *and* by the `jobs.max_attempts` column. The
# duplication is deliberate: a queue recreated with defaults cannot produce an
# unbounded retry storm against a paid provider, because the row refuses first.
##############################################################################

resource "google_cloud_tasks_queue" "media" {
  name     = "tutortwin-media-${var.environment}"
  location = var.region

  rate_limits {
    max_concurrent_dispatches = var.queue_max_concurrent
    max_dispatches_per_second = var.queue_max_per_second
  }

  retry_config {
    max_attempts       = 3
    min_backoff        = "30s"
    max_backoff        = "600s"
    max_doublings      = 4
    max_retry_duration = "3600s"
  }
}

# The queue calls the API as `invoker`, and only `invoker` may call it. This is
# what makes /internal/jobs/run authenticated at the platform layer as well as
# in the application.
resource "google_cloud_run_v2_service_iam_member" "queue_invokes_api" {
  name     = google_cloud_run_v2_service.api.name
  location = google_cloud_run_v2_service.api.location
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.invoker.email}"
}

resource "google_project_iam_member" "api_enqueues" {
  project = var.project_id
  role    = "roles/cloudtasks.enqueuer"
  member  = "serviceAccount:${google_service_account.api.email}"
}

# Creating a task with an OIDC token requires acting as the identity that token
# is minted for.
resource "google_service_account_iam_member" "api_acts_as_invoker" {
  service_account_id = google_service_account.invoker.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.api.email}"
}

##############################################################################
# Retention sweep
#
# Cloud Scheduler, not a cron container: there is no always-on worker in this
# architecture, and a nightly HTTP call needs no process of its own.
##############################################################################

resource "google_cloud_scheduler_job" "retention" {
  name        = "tutortwin-retention-${var.environment}"
  description = "Delete expired media rows and their extracted text"
  schedule    = "17 3 * * *"
  time_zone   = "Etc/UTC"

  retry_config {
    retry_count = 3
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.api.uri}/internal/retention/sweep"
    headers = {
      "Content-Type" = "application/json"
    }
    body = base64encode(jsonencode({ batch_size = 200 }))

    oidc_token {
      service_account_email = google_service_account.invoker.email
      audience              = google_cloud_run_v2_service.api.uri
    }
  }
}

##############################################################################
# Budget alert
#
# Optional, and worth the two resources: the cost controls inside the
# application bound what *it* decides to spend. This bounds what the invoice can
# be if one of them is wrong.
##############################################################################

resource "google_billing_budget" "monthly" {
  count = var.billing_account == "" ? 0 : 1

  billing_account = var.billing_account
  display_name    = "TutorTwin ${var.environment}"

  budget_filter {
    projects = ["projects/${var.project_number}"]
  }

  amount {
    specified_amount {
      currency_code = var.budget_currency
      units         = tostring(var.monthly_budget)
    }
  }

  dynamic "threshold_rules" {
    for_each = [0.5, 0.9, 1.0]
    content {
      threshold_percent = threshold_rules.value
    }
  }
}
