##############################################################################
# Outputs.
#
# These are the values a deploy needs *after* apply, and the ones that would
# otherwise be copied by hand from the console into an environment variable -
# which is where a queue that pushes to the wrong URL comes from.
##############################################################################

output "api_url" {
  description = "Base URL of the API service."
  value       = google_cloud_run_v2_service.api.uri
}

output "admin_url" {
  description = "Base URL of the control plane."
  value       = google_cloud_run_v2_service.admin.uri
}

output "tasks_target_url" {
  description = "Set as TUTORTWIN_TASKS_TARGET_URL. Also the OIDC audience."
  value       = "${google_cloud_run_v2_service.api.uri}/internal/jobs/run"
}

output "oidc_audience" {
  description = "Set as TUTORTWIN_OIDC_AUDIENCE. Cloud Tasks mints the token for the service root, and Cloud Run verifies it against the same value."
  value       = google_cloud_run_v2_service.api.uri
}

output "tasks_service_account" {
  description = "Set as TUTORTWIN_TASKS_SERVICE_ACCOUNT and TUTORTWIN_OIDC_SERVICE_ACCOUNT."
  value       = google_service_account.invoker.email
}

output "tasks_queue" {
  description = "Set as TUTORTWIN_TASKS_QUEUE."
  value       = google_cloud_tasks_queue.media.name
}

output "artifact_repository" {
  description = "Docker push target."
  value       = "${google_artifact_registry_repository.images.location}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "api_service_account" {
  value = google_service_account.api.email
}
