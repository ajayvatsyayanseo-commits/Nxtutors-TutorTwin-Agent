import { expect, type Page } from "@playwright/test";

/**
 * Shared helpers for the end-to-end suite.
 *
 * The fixture data is created through the **real API**, using the real
 * bootstrap administrator, so the tests exercise the same authorisation path an
 * operator does. Nothing writes to the database directly: a test that bypasses
 * the API would pass even if every endpoint were broken.
 */

export const API_URL = process.env.TUTORTWIN_API_URL ?? "http://127.0.0.1:8000";

export const SUPER_ADMIN = {
  email: process.env.E2E_ADMIN_EMAIL ?? "e2e-super@example.com",
  password: process.env.E2E_ADMIN_PASSWORD ?? "e2e-super-admin-password",
};

export const SUPPORT_ADMIN = {
  email: "e2e-support@example.com",
  password: "e2e-support-password-1",
};

export interface ApiSession {
  token: string;
  csrf: string;
}

/** Sign in to the API directly, for seeding. */
export async function apiLogin(
  email: string,
  password: string,
): Promise<ApiSession> {
  const response = await fetch(`${API_URL}/v1/admin/auth/login`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!response.ok) {
    throw new Error(`Seed login failed for ${email}: ${response.status} ${await response.text()}`);
  }
  const cookie = response.headers.get("set-cookie") ?? "";
  const match = /tt_admin_session=([^;]+)/.exec(cookie);
  if (!match?.[1]) throw new Error("API did not issue a session cookie.");
  const body = (await response.json()) as { csrf_token: string };
  return { token: match[1], csrf: body.csrf_token };
}

export async function apiCall<T>(
  session: ApiSession,
  path: string,
  init: { method?: string; body?: unknown } = {},
): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    method: init.method ?? "GET",
    headers: {
      "content-type": "application/json",
      "x-admin-session": session.token,
      "x-admin-csrf": session.csrf,
    },
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
  });
  if (!response.ok) {
    throw new Error(`${init.method ?? "GET"} ${path} -> ${response.status}: ${await response.text()}`);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** Sign in through the browser, the way an operator does. */
export async function signIn(
  page: Page,
  who: { email: string; password: string } = SUPER_ADMIN,
): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Email").fill(who.email);
  await page.getByLabel("Password").fill(who.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 20_000 });
}

/**
 * Fill a high-risk form's reason and confirmation, then submit.
 *
 * Every dangerous action in the UI carries both; a helper keeps the tests
 * honest about that rather than letting one quietly skip the confirmation.
 */
export async function confirmHighRisk(
  scope: Page | ReturnType<Page["locator"]>,
  reason: string,
  submitName: string | RegExp,
): Promise<void> {
  await scope.getByLabel(/Reason/).fill(reason);
  // By name, not "the checkbox in this scope": a panel may hold others — the
  // model routing form has an "active" toggle beside the confirmation — and
  // ticking whichever came first is how a test silently confirms nothing.
  await scope.getByRole("checkbox", { name: /^I understand/ }).check();
  await scope.getByRole("button", { name: submitName }).click();
}

export async function expectSuccess(page: Page, text?: string | RegExp): Promise<void> {
  const alert = page.getByRole("status").filter({ hasText: text ?? /./ }).first();
  await expect(alert).toBeVisible({ timeout: 20_000 });
}

/**
 * The application's own error banner.
 *
 * Not `getByRole("alert")`: Next renders a permanently-present, usually empty
 * route announcer with that role, so the bare query is ambiguous on every page.
 */
export function errorBanner(
  scope: Page | ReturnType<Page["locator"]>,
): ReturnType<Page["locator"]> {
  return scope.locator(".alert-error");
}
