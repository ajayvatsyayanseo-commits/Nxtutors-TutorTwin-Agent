import "server-only";

import { forbidden, redirect, unstable_rethrow } from "next/navigation";

import { readSession } from "./session";
import type { AdminActor } from "./types";

/**
 * The only path from this application to the TutorTwin API.
 *
 * **The browser never talks to the API, and never talks to Postgres.** Every
 * request originates here, on the server, with the session token attached from
 * an httpOnly cookie. That is what makes the token unstealable by page script
 * and what keeps authorisation on the server where it is enforced.
 *
 * Errors are normalised into `ApiError` so a page can distinguish "you are
 * logged out" from "you may not do that" from "the API is down" - three very
 * different things to show an operator.
 */

const BASE_URL = process.env.TUTORTWIN_API_URL ?? "http://127.0.0.1:8000";
const TIMEOUT_MS = Number(process.env.TUTORTWIN_API_TIMEOUT_MS ?? 15000);

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly fields?: string[],
  ) {
    super(message);
    this.name = "ApiError";
  }

  get isUnauthenticated(): boolean {
    return this.status === 401;
  }

  get isForbidden(): boolean {
    return this.status === 403;
  }

  get isNotFound(): boolean {
    return this.status === 404;
  }
}

interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined | null>;
  /** Login is the one call that has no session yet. */
  anonymous?: boolean;
  /**
   * Return the 401 instead of redirecting. Only `currentActor()` sets it: the
   * layout has to be able to ask "is anyone signed in?" and get an answer.
   */
  allowUnauthenticated?: boolean;
  /** Set by the login route so the response headers can be read. */
  raw?: boolean;
}

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  const url = new URL(path, BASE_URL);
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === undefined || value === null || value === "") continue;
    url.searchParams.set(key, String(value));
  }
  return url.toString();
}

async function parseError(response: Response): Promise<ApiError> {
  let code = "UNKNOWN";
  let message = `Request failed with status ${response.status}.`;
  let fields: string[] | undefined;
  try {
    const payload = (await response.json()) as {
      error?: { code?: string; message?: string; fields?: string[] };
      detail?: unknown;
    };
    if (payload.error) {
      code = payload.error.code ?? code;
      message = payload.error.message ?? message;
      fields = payload.error.fields;
    } else if (payload.detail) {
      // FastAPI's own validation shape, before our handler formats it.
      code = "VALIDATION_FAILED";
      message = "The request was rejected as invalid.";
    }
  } catch {
    // A non-JSON body means the API is not answering normally; the status is
    // the only reliable signal, and it is already in the error.
  }
  return new ApiError(response.status, code, message, fields);
}

export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const {
    method = "GET",
    body,
    query,
    anonymous = false,
    allowUnauthenticated = false,
  } = options;

  const headers: Record<string, string> = { accept: "application/json" };
  if (body !== undefined) headers["content-type"] = "application/json";

  if (!anonymous) {
    const session = await readSession();
    if (!session) {
      if (!allowUnauthenticated) redirect("/login");
      throw new ApiError(401, "UNAUTHENTICATED", "Your session has ended.");
    }
    headers["x-admin-session"] = session.token;
    // Sent on every request, not only mutations: one rule is easier to keep
    // right than a rule with an exception.
    headers["x-admin-csrf"] = session.csrf;
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const response = await fetch(buildUrl(path, query), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
      // Admin data is never cached: a stale quota or flag reading is worse than
      // a slow one.
      cache: "no-store",
    });

    if (!response.ok) {
      // A **read** the API refuses is a page this operator may not open, and it
      // must answer with a status that says so. A refused **write** is not: the
      // operator is on a page they may look at, and the form should tell them
      // why the button did not work rather than replacing the page they were
      // reading. Mutations therefore keep the throwing `ApiError` path.
      if (response.status === 403 && method === "GET") forbidden();
      // An expired session is the ordinary end of a working day, not a fault.
      // Sending the operator to the login page beats logging an error and
      // rendering a panel that says the API refused them.
      if (response.status === 401 && !allowUnauthenticated) redirect("/login");
      throw await parseError(response);
    }
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  } catch (error) {
    // `forbidden()` signals by throwing; swallowing it into a 503 would turn a
    // refusal into "the API is down".
    unstable_rethrow(error);
    if (error instanceof ApiError) throw error;
    if (error instanceof Error && error.name === "AbortError") {
      throw new ApiError(504, "TIMEOUT", "The API did not respond in time.");
    }
    throw new ApiError(
      503,
      "DEPENDENCY_UNAVAILABLE",
      "The TutorTwin API could not be reached.",
    );
  } finally {
    clearTimeout(timer);
  }
}

/** Login is separate: it has no session and returns tokens rather than data. */
export async function apiLogin(
  email: string,
  password: string,
): Promise<{
  actor: AdminActor;
  csrf_token: string;
  expires_at: string;
  session_token: string;
}> {
  const response = await fetch(buildUrl("/v1/admin/auth/login"), {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify({ email, password }),
    cache: "no-store",
  });

  if (!response.ok) throw await parseError(response);

  const payload = (await response.json()) as {
    actor: AdminActor;
    csrf_token: string;
    expires_at: string;
  };

  // The API sets its session as a cookie; this server reads it out of the
  // Set-Cookie header and stores it in its own httpOnly cookie instead, so the
  // browser never holds an API credential.
  const setCookie = response.headers.get("set-cookie") ?? "";
  const match = /tt_admin_session=([^;]+)/.exec(setCookie);
  if (!match?.[1]) {
    throw new ApiError(502, "BAD_GATEWAY", "The API did not issue a session.");
  }

  return { ...payload, session_token: match[1] };
}

export async function apiLogout(): Promise<void> {
  try {
    await apiFetch("/v1/admin/auth/logout", { method: "POST" });
  } catch (error) {
    // A logout that fails server-side must still clear the local cookies;
    // leaving the operator "logged in" to a dead session helps nobody.
    if (!(error instanceof ApiError)) throw error;
  }
}
