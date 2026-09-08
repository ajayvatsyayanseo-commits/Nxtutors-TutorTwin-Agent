"use server";

import { revalidatePath } from "next/cache";

import { ApiError, apiFetch } from "./api";
import type { FormState } from "./auth-actions";

/**
 * Server actions for every mutation the control plane performs.
 *
 * Two rules hold throughout:
 *
 * **The reason and the confirmation come from the form, and the API checks them
 * again.** A hand-crafted request that omits either is refused server-side, so
 * these fields are not a UI ritual.
 *
 * **A failure returns a message; it does not throw into a crash page.** An
 * operator who mistyped a plan code should see why, on the form, with what they
 * typed still there.
 */

function fail(error: unknown): FormState {
  if (error instanceof ApiError) {
    if (error.isForbidden) {
      return { error: `${error.message} (your role does not permit this)` };
    }
    return { error: error.message };
  }
  throw error;
}

function requireConfirmed(formData: FormData): FormState | null {
  if (formData.get("confirm") !== "yes") {
    return { error: "Tick the confirmation box to proceed." };
  }
  const reason = String(formData.get("reason") ?? "").trim();
  if (reason.length < 8) {
    return { error: "Give a reason of at least 8 characters. It is recorded in the audit log." };
  }
  return null;
}

function reason(formData: FormData): string {
  return String(formData.get("reason") ?? "").trim();
}

// --- students -----------------------------------------------------------------

export async function overrideEntitlementAction(
  _previous: FormState,
  formData: FormData,
): Promise<FormState> {
  const invalid = requireConfirmed(formData);
  if (invalid) return invalid;

  const studentId = String(formData.get("student_id") ?? "");
  try {
    await apiFetch(`/v1/admin/students/${studentId}/entitlement`, {
      method: "POST",
      body: {
        plan_code: String(formData.get("plan_code") ?? "").trim(),
        status: String(formData.get("status") ?? "ACTIVE"),
        reason: reason(formData),
        confirm: true,
      },
    });
  } catch (error) {
    return fail(error);
  }

  revalidatePath(`/students/${studentId}`);
  return { success: "Entitlement updated and recorded in the audit log." };
}

export async function grantSubscriptionAction(
  _previous: FormState,
  formData: FormData,
): Promise<FormState> {
  const invalid = requireConfirmed(formData);
  if (invalid) return invalid;

  const days = Number(formData.get("days") ?? 30);
  if (!Number.isInteger(days) || days < 1 || days > 3660) {
    return { error: "Days must be a whole number between 1 and 3660." };
  }

  let response: { plan_code: string; ends_at: string; notified: boolean };
  try {
    response = await apiFetch<{ plan_code: string; ends_at: string; notified: boolean }>(
      "/v1/admin/students/grant-subscription",
      {
        method: "POST",
        body: {
          whatsapp_number: String(formData.get("whatsapp_number") ?? "").trim(),
          student_name: String(formData.get("student_name") ?? "").trim(),
          tutor_name: String(formData.get("tutor_name") ?? "TutorTwin").trim(),
          subject: String(formData.get("subject") ?? "General").trim(),
          plan_code: String(formData.get("plan_code") ?? "PRO").trim(),
          days,
          // Unticked means "fix a mistake quietly". A student who is not told
          // they have a subscription behaves exactly like one who has none, so
          // notifying is the default and silence is the deliberate choice.
          notify: formData.get("notify") === "yes",
          reason: reason(formData),
          confirm: true,
        },
      },
    );
  } catch (error) {
    return fail(error);
  }

  revalidatePath("/students");
  revalidatePath("/audit");

  const until = new Date(response.ends_at).toLocaleDateString("en-IN", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
  return {
    success: response.notified
      ? `Subscription granted until ${until}. A WhatsApp message has been sent.`
      : `Subscription granted until ${until}. No WhatsApp message was sent.`,
  };
}

export async function resetQuotaAction(
  _previous: FormState,
  formData: FormData,
): Promise<FormState> {
  const invalid = requireConfirmed(formData);
  if (invalid) return invalid;

  const studentId = String(formData.get("student_id") ?? "");
  try {
    await apiFetch("/v1/admin/quota/reset", {
      method: "POST",
      body: { subject_id: studentId, reason: reason(formData), confirm: true },
    });
  } catch (error) {
    return fail(error);
  }

  revalidatePath(`/students/${studentId}`);
  return { success: "Quota reset recorded. Spend history is unchanged." };
}

export async function deleteStudentDataAction(
  _previous: FormState,
  formData: FormData,
): Promise<FormState> {
  const invalid = requireConfirmed(formData);
  if (invalid) return invalid;

  const studentId = String(formData.get("student_id") ?? "");
  try {
    await apiFetch(`/v1/admin/students/${studentId}/delete-data`, {
      method: "POST",
      body: {
        confirm_identity: String(formData.get("confirm_identity") ?? "").trim(),
        reason: reason(formData),
        confirm: true,
      },
    });
  } catch (error) {
    return fail(error);
  }

  revalidatePath(`/students/${studentId}`);
  return { success: "Learning data deleted. The audit record of the deletion remains." };
}

// --- tutors -------------------------------------------------------------------

export async function createTutorAction(
  _previous: FormState,
  formData: FormData,
): Promise<FormState> {
  const displayName = String(formData.get("display_name") ?? "").trim();
  if (!displayName) return { error: "Give the tutor a name." };

  try {
    await apiFetch("/v1/admin/tutors", {
      method: "POST",
      body: { display_name: displayName },
    });
  } catch (error) {
    return fail(error);
  }

  revalidatePath("/tutors");
  return { success: `Tutor "${displayName}" created.` };
}

export async function createPersonaDraftAction(
  _previous: FormState,
  formData: FormData,
): Promise<FormState> {
  const tutorId = String(formData.get("tutor_id") ?? "");
  const list = (field: string) =>
    String(formData.get(field) ?? "")
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean);
  const subjects = list("subjects");

  try {
    await apiFetch(`/v1/admin/tutors/${tutorId}/personas`, {
      method: "POST",
      body: {
        display_name: String(formData.get("display_name") ?? "").trim(),
        avatar_url: String(formData.get("avatar_url") ?? "").trim(),
        subjects,
        tone: String(formData.get("tone") ?? "").trim(),
        pedagogy_mode: String(formData.get("pedagogy_mode") ?? "GUIDED"),
        language: String(formData.get("language") ?? "en").trim(),
        response_style: String(formData.get("response_style") ?? "").trim(),
        notification_preference: String(formData.get("notification_preference") ?? "none"),
        signature_phrases: list("signature_phrases"),
      },
    });
  } catch (error) {
    return fail(error);
  }

  revalidatePath(`/tutors/${tutorId}`);
  return { success: "Draft saved. It is inert until you activate it." };
}

export async function activatePersonaAction(
  _previous: FormState,
  formData: FormData,
): Promise<FormState> {
  const invalid = requireConfirmed(formData);
  if (invalid) return invalid;

  const tutorId = String(formData.get("tutor_id") ?? "");
  const versionId = String(formData.get("version_id") ?? "");
  try {
    await apiFetch(`/v1/admin/tutors/${tutorId}/personas/${versionId}/activate`, {
      method: "POST",
      body: { reason: reason(formData), confirm: true },
    });
  } catch (error) {
    return fail(error);
  }

  revalidatePath(`/tutors/${tutorId}`);
  return { success: "Persona activated. Every assigned student hears it on their next message." };
}

export async function assignStudentAction(
  _previous: FormState,
  formData: FormData,
): Promise<FormState> {
  const tutorId = String(formData.get("tutor_id") ?? "");
  const subjectId = String(formData.get("subject_id") ?? "").trim();
  const why = reason(formData);
  if (!subjectId) return { error: "Enter the student id to assign." };
  if (!why) return { error: "Give a reason. It is recorded in the audit log." };

  try {
    await apiFetch(`/v1/admin/tutors/${tutorId}/students`, {
      method: "POST",
      body: { subject_id: subjectId, reason: why },
    });
  } catch (error) {
    return fail(error);
  }

  // The assignment shows on both pages, so both are invalidated. Revalidating
  // only the page the operator happened to be on leaves the other one lying.
  revalidatePath(`/tutors/${tutorId}`);
  revalidatePath(`/students/${subjectId}`);
  revalidatePath("/students");
  return { success: "Student assigned. Any previous active assignment was retired." };
}

// --- plans, models, prompts, flags -------------------------------------------

export async function upsertPlanAction(
  _previous: FormState,
  formData: FormData,
): Promise<FormState> {
  const invalid = requireConfirmed(formData);
  if (invalid) return invalid;

  let features: unknown = {};
  let limits: unknown = {};
  try {
    features = JSON.parse(String(formData.get("features") || "{}"));
    limits = JSON.parse(String(formData.get("limits") || "{}"));
  } catch {
    return { error: "Features and limits must be valid JSON objects." };
  }

  try {
    await apiFetch("/v1/admin/plans", {
      method: "POST",
      body: {
        plan_code: String(formData.get("plan_code") ?? "").trim(),
        allows_paid_ai: formData.get("allows_paid_ai") === "yes",
        features,
        limits,
        reason: reason(formData),
        confirm: true,
      },
    });
  } catch (error) {
    return fail(error);
  }

  revalidatePath("/plans");
  return { success: "New plan version published. Previous versions are retained." };
}

export async function upsertModelRouteAction(
  _previous: FormState,
  formData: FormData,
): Promise<FormState> {
  const invalid = requireConfirmed(formData);
  if (invalid) return invalid;

  try {
    await apiFetch("/v1/admin/models", {
      method: "POST",
      body: {
        model_alias: String(formData.get("model_alias") ?? ""),
        provider: String(formData.get("provider") ?? ""),
        model_id: String(formData.get("model_id") ?? "").trim(),
        is_active: formData.get("is_active") === "yes",
        input_cost_micros_per_1k: Number(formData.get("input_cost") ?? 0),
        output_cost_micros_per_1k: Number(formData.get("output_cost") ?? 0),
        rate_version: String(formData.get("rate_version") ?? "v1").trim(),
        reason: reason(formData),
        confirm: true,
      },
    });
  } catch (error) {
    return fail(error);
  }

  revalidatePath("/models");
  return { success: "Route saved. New calls use it; existing ledger rows keep their rate." };
}

export async function createPromptDraftAction(
  _previous: FormState,
  formData: FormData,
): Promise<FormState> {
  const why = reason(formData);
  if (!why) return { error: "Give a reason for this draft." };

  try {
    await apiFetch("/v1/admin/prompts", {
      method: "POST",
      body: {
        block_key: String(formData.get("block_key") ?? "").trim(),
        body: String(formData.get("body") ?? ""),
        reason: why,
      },
    });
  } catch (error) {
    return fail(error);
  }

  revalidatePath("/prompts");
  return { success: "Draft created. It has no effect until activated." };
}

export async function activatePromptAction(
  _previous: FormState,
  formData: FormData,
): Promise<FormState> {
  const invalid = requireConfirmed(formData);
  if (invalid) return invalid;

  try {
    await apiFetch(`/v1/admin/prompts/${String(formData.get("version_id"))}/activate`, {
      method: "POST",
      body: { reason: reason(formData), confirm: true },
    });
  } catch (error) {
    return fail(error);
  }

  revalidatePath("/prompts");
  return { success: "Version activated. The previous active version is retired, not deleted." };
}

export async function setFlagAction(
  _previous: FormState,
  formData: FormData,
): Promise<FormState> {
  const invalid = requireConfirmed(formData);
  if (invalid) return invalid;

  const key = String(formData.get("key") ?? "");
  const enabled = formData.get("enabled") === "yes";
  try {
    await apiFetch(`/v1/admin/flags/${key}`, {
      method: "POST",
      body: { enabled, reason: reason(formData), confirm: true },
    });
  } catch (error) {
    return fail(error);
  }

  revalidatePath("/flags");
  return {
    success: enabled
      ? `"${key}" enabled.`
      : `"${key}" disabled. The capability stops at the next request.`,
  };
}

// --- documents ----------------------------------------------------------------

export async function reprocessDocumentAction(
  _previous: FormState,
  formData: FormData,
): Promise<FormState> {
  const invalid = requireConfirmed(formData);
  if (invalid) return invalid;

  const documentId = String(formData.get("document_id") ?? "");
  try {
    const result = await apiFetch<{ chunks_to_reembed: string }>(
      `/v1/admin/documents/${documentId}/reprocess`,
      { method: "POST", body: { reason: reason(formData), confirm: true } },
    );
    revalidatePath(`/documents/${documentId}`);
    return {
      success: `Queued for re-ingestion. ${result.chunks_to_reembed} chunk(s) will be re-embedded.`,
    };
  } catch (error) {
    return fail(error);
  }
}

export async function deleteDocumentAction(
  _previous: FormState,
  formData: FormData,
): Promise<FormState> {
  const invalid = requireConfirmed(formData);
  if (invalid) return invalid;

  const documentId = String(formData.get("document_id") ?? "");
  try {
    await apiFetch(`/v1/admin/documents/${documentId}/delete`, {
      method: "POST",
      body: { reason: reason(formData), confirm: true },
    });
  } catch (error) {
    return fail(error);
  }

  revalidatePath("/documents");
  return { success: "Document removed from retrieval." };
}

// --- jobs ---------------------------------------------------------------------

export async function retryJobAction(
  _previous: FormState,
  formData: FormData,
): Promise<FormState> {
  const why = reason(formData);
  if (!why) return { error: "Give a reason for the retry." };

  const jobId = String(formData.get("job_id") ?? "");
  try {
    await apiFetch(`/v1/admin/jobs/${jobId}/retry`, {
      method: "POST",
      body: { reason: why },
    });
  } catch (error) {
    return fail(error);
  }

  revalidatePath("/jobs");
  return { success: "Job re-armed. The attempt history is kept." };
}

export async function cancelJobAction(
  _previous: FormState,
  formData: FormData,
): Promise<FormState> {
  const why = reason(formData);
  if (!why) return { error: "Give a reason for the cancellation." };

  const jobId = String(formData.get("job_id") ?? "");
  try {
    await apiFetch(`/v1/admin/jobs/${jobId}/cancel`, {
      method: "POST",
      body: { reason: why },
    });
  } catch (error) {
    return fail(error);
  }

  revalidatePath("/jobs");
  return { success: "Job cancelled." };
}

// --- administrators -----------------------------------------------------------

export async function createAdminAction(
  _previous: FormState,
  formData: FormData,
): Promise<FormState> {
  const invalid = requireConfirmed(formData);
  if (invalid) return invalid;

  try {
    await apiFetch("/v1/admin/admins", {
      method: "POST",
      body: {
        email: String(formData.get("email") ?? "").trim(),
        display_name: String(formData.get("display_name") ?? "").trim(),
        role: String(formData.get("role") ?? ""),
        password: String(formData.get("password") ?? ""),
        reason: reason(formData),
        confirm: true,
      },
    });
  } catch (error) {
    return fail(error);
  }

  revalidatePath("/admins");
  return { success: "Administrator created. They must change the password at first login." };
}

export async function changeAdminRoleAction(
  _previous: FormState,
  formData: FormData,
): Promise<FormState> {
  const invalid = requireConfirmed(formData);
  if (invalid) return invalid;

  const adminId = String(formData.get("admin_id") ?? "");
  try {
    await apiFetch(`/v1/admin/admins/${adminId}/role`, {
      method: "POST",
      body: { role: String(formData.get("role") ?? ""), reason: reason(formData), confirm: true },
    });
  } catch (error) {
    return fail(error);
  }

  revalidatePath("/admins");
  return { success: "Role changed. Their existing sessions were revoked." };
}

export async function setAdminStatusAction(
  _previous: FormState,
  formData: FormData,
): Promise<FormState> {
  const invalid = requireConfirmed(formData);
  if (invalid) return invalid;

  const adminId = String(formData.get("admin_id") ?? "");
  try {
    await apiFetch(`/v1/admin/admins/${adminId}/status`, {
      method: "POST",
      body: {
        status: String(formData.get("status") ?? "DISABLED"),
        reason: reason(formData),
        confirm: true,
      },
    });
  } catch (error) {
    return fail(error);
  }

  revalidatePath("/admins");
  return { success: "Account status updated." };
}
