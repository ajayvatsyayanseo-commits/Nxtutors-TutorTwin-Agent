/**
 * Public runtime configuration.
 *
 * Everything here is `NEXT_PUBLIC_*` and therefore baked into the browser
 * bundle - so nothing secret may ever be added to this file. The Cashfree
 * *secret* key lives only in the Python service; the browser gets a
 * single-use payment session id and nothing else.
 */

export const API_URL: string =
  process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

/**
 * Must match the gateway the server created the order against. A production
 * session id opened in sandbox mode fails with an error that names neither
 * cause, so this defaults to sandbox: a test payment against production keys
 * is a worse first failure than the reverse.
 */
export const CASHFREE_MODE: "sandbox" | "production" =
  process.env.NEXT_PUBLIC_CASHFREE_MODE === "production" ? "production" : "sandbox";

export const SITE_URL: string =
  process.env.NEXT_PUBLIC_SITE_URL ?? "https://nxtutortwin.nxtutors.com";
