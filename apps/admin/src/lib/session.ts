import "server-only";

import { cookies } from "next/headers";

import type { AdminActor } from "./types";

/**
 * Session handling for the control plane.
 *
 * **The browser never receives the API session token or the CSRF token.** Both
 * live in httpOnly cookies that only this Next.js server reads, and every call
 * to the Python API is made server-side. A cross-site script that runs in the
 * admin page therefore has nothing to steal: `document.cookie` is empty of
 * anything useful, and there is no token in a JS variable to read.
 *
 * The CSRF token is stored in its own httpOnly cookie rather than exposed to the
 * page, because the mutation path is a server action. Next signs and verifies
 * its own action requests, and the token is attached by the server on the way
 * out - so a cross-site form post carries neither.
 */

export const SESSION_COOKIE = "tt_admin";
export const CSRF_COOKIE = "tt_admin_csrf";

export interface StoredSession {
  token: string;
  csrf: string;
}

/** Cookie options that hold on every environment we deploy to. */
function cookieOptions(expires?: Date) {
  return {
    httpOnly: true,
    // `secure` follows the deployment, not the code: local development is http.
    secure: process.env.NODE_ENV === "production",
    sameSite: "strict" as const,
    path: "/",
    ...(expires ? { expires } : {}),
  };
}

export async function readSession(): Promise<StoredSession | null> {
  const jar = await cookies();
  const token = jar.get(SESSION_COOKIE)?.value;
  const csrf = jar.get(CSRF_COOKIE)?.value;
  if (!token || !csrf) return null;
  return { token, csrf };
}

export async function writeSession(
  session: StoredSession,
  expiresAt: string,
): Promise<void> {
  const jar = await cookies();
  const expires = new Date(expiresAt);
  jar.set(SESSION_COOKIE, session.token, cookieOptions(expires));
  jar.set(CSRF_COOKIE, session.csrf, cookieOptions(expires));
}

export async function clearSession(): Promise<void> {
  const jar = await cookies();
  jar.delete(SESSION_COOKIE);
  jar.delete(CSRF_COOKIE);
}

/** Permission checks used to hide UI. The API enforces the same rules. */
export function can(actor: AdminActor | null, permission: string): boolean {
  return actor?.permissions.includes(permission) ?? false;
}

export function canAny(actor: AdminActor | null, ...permissions: string[]): boolean {
  return permissions.some((permission) => can(actor, permission));
}
