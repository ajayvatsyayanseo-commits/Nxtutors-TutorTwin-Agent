import { NextResponse, type NextRequest } from "next/server";

/**
 * Content-Security-Policy, with a per-request nonce.
 *
 * The static headers live in `next.config.ts`; this one cannot, because a nonce
 * that is the same on every response is not a nonce. Next reads the CSP off the
 * request headers and stamps the same nonce onto the bootstrap scripts it
 * injects, which is what lets `script-src` refuse everything else.
 *
 * **No `unsafe-inline` for scripts.** A control plane that relaxes that to make
 * a chart library work has traded its main defence against an injected script
 * for a convenience — which is why the dashboard draws its bars with a div.
 *
 * `style-src` does allow inline, because React writes `style` attributes for the
 * handful of one-off widths and the alternative is a class for every number.
 * An inline style cannot exfiltrate a session the way an inline script can, and
 * the session token is not in the page at all: it lives in an httpOnly cookie
 * this server reads.
 *
 * `connect-src 'self'` is deliberate: the browser has no reason to reach the
 * TutorTwin API directly, and every page here fetches server-side.
 */
export function middleware(request: NextRequest) {
  const nonce = crypto.randomUUID().replaceAll("-", "");

  const csp = [
    "default-src 'self'",
    // No `'strict-dynamic'`.
    //
    // It reads as the stricter choice and is the opposite here: `strict-dynamic`
    // tells the browser to IGNORE `'self'` and trust only nonce-carrying
    // scripts plus whatever those load. Next's initial <script> tags do carry
    // the nonce, but Turbopack's runtime fetches further chunks itself, and
    // those were refused - so the bundle never bootstrapped, React never
    // hydrated, and every form in the control plane silently did nothing. A
    // submit fell back to a native GET and bounced to /login.
    //
    // `'self'` plus a nonce keeps the property that actually matters: no
    // `unsafe-inline`, so an injected <script> tag still cannot execute. The
    // residual exposure is a same-origin script URL, and this app serves no
    // user-supplied files to be that URL.
    `script-src 'self' 'nonce-${nonce}'`,
    "style-src 'self' 'unsafe-inline'",
    // Avatar URLs are operator-supplied and point wherever the tutor's picture
    // is hosted. An image cannot execute; the CSP still forbids everything else.
    "img-src 'self' data: https:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "object-src 'none'",
    "upgrade-insecure-requests",
  ].join("; ");

  const headers = new Headers(request.headers);
  headers.set("x-nonce", nonce);
  headers.set("content-security-policy", csp);
  // A server component cannot ask which URL it is rendering. The authenticated
  // layout needs it to decide whether this operator may open the section at all,
  // and deciding that in the shell is what makes the refusal a 403 rather than a
  // 200 with an error panel inside it.
  headers.set("x-pathname", request.nextUrl.pathname);

  const response = NextResponse.next({ request: { headers } });
  response.headers.set("content-security-policy", csp);
  return response;
}

export const config = {
  matcher: [
    // Everything except Next's own static output and the favicon: those are
    // immutable files, and running a nonce generator for each one is waste.
    {
      source: "/((?!_next/static|_next/image|favicon.ico).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
