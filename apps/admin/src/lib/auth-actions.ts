"use server";

import { redirect } from "next/navigation";

import { ApiError, apiFetch, apiLogin, apiLogout } from "./api";
import { clearSession, writeSession } from "./session";
import type { AdminActor } from "./types";

/**
 * Server actions for authentication.
 *
 * Every mutation in this application is a server action. That is the security
 * design, not a style preference: the session token lives in an httpOnly cookie
 * this server reads, so there is no fetch from the browser to authenticate and
 * nothing for page script to steal. Next.js also signs and verifies its own
 * action requests, which closes the cross-site POST that a plain form would open.
 */

export interface FormState {
  error?: string;
  success?: string;
}

export async function loginAction(
  _previous: FormState,
  formData: FormData,
): Promise<FormState> {
  const email = String(formData.get("email") ?? "").trim();
  const password = String(formData.get("password") ?? "");

  if (!email || !password) {
    return { error: "Enter your email and password." };
  }

  try {
    const result = await apiLogin(email, password);
    await writeSession(
      { token: result.session_token, csrf: result.csrf_token },
      result.expires_at,
    );
  } catch (error) {
    if (error instanceof ApiError) {
      // The API answers every failed login identically; this passes that through
      // rather than guessing at a friendlier, more informative message.
      if (error.status === 429) {
        return { error: "Too many attempts. Wait a few minutes and try again." };
      }
      return { error: error.isUnauthenticated ? "Invalid email or password." : error.message };
    }
    throw error;
  }

  redirect("/");
}

export async function logoutAction(): Promise<void> {
  await apiLogout();
  await clearSession();
  redirect("/login");
}

export async function changePasswordAction(
  _previous: FormState,
  formData: FormData,
): Promise<FormState> {
  const current = String(formData.get("current_password") ?? "");
  const next = String(formData.get("new_password") ?? "");
  const confirm = String(formData.get("confirm_password") ?? "");

  if (next !== confirm) return { error: "The two new passwords do not match." };
  if (next.length < 12) return { error: "New password must be at least 12 characters." };

  try {
    await apiFetch("/v1/admin/auth/change-password", {
      method: "POST",
      body: { current_password: current, new_password: next },
    });
  } catch (error) {
    if (error instanceof ApiError) return { error: error.message };
    throw error;
  }

  // Changing a password revokes every session for the account, this one
  // included, so the operator must sign in again with the new credential.
  await clearSession();
  redirect("/login?changed=1");
}

/** The signed-in operator, or null. Used by the layout to build navigation. */
export async function currentActor(): Promise<AdminActor | null> {
  try {
    return await apiFetch<AdminActor>("/v1/admin/auth/me", {
      allowUnauthenticated: true,
    });
  } catch (error) {
    if (error instanceof ApiError && error.isUnauthenticated) return null;
    throw error;
  }
}
