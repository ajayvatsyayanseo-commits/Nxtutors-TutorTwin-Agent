import type { NextConfig } from "next";

/**
 * The public site. No login, no session cookie, nothing to steal.
 *
 * That is exactly why it lives on its own subdomain and not under the admin
 * one: this page loads Cashfree's checkout SDK, which is third-party
 * JavaScript we do not control. On a shared origin an XSS here could read the
 * admin session cookie. Separate origins make the browser enforce that
 * boundary for us, permanently and for free.
 *
 * The CSP below is correspondingly narrow given it must host that SDK:
 * scripts come from us and Cashfree only, and `frame-ancestors 'none'` keeps
 * the payment page out of anybody else's iframe.
 */
const CSP = [
  "default-src 'self'",
  // Cashfree's checkout SDK, and nothing else off-origin.
  "script-src 'self' 'unsafe-inline' https://sdk.cashfree.com",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: https:",
  "font-src 'self' data:",
  // The API host, plus Cashfree's own endpoints the SDK calls.
  "connect-src 'self' https://api.nxtutors.com https://api.cashfree.com https://sandbox.cashfree.com",
  "frame-src https://sdk.cashfree.com https://payments.cashfree.com",
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self' https://payments.cashfree.com",
  "object-src 'none'",
].join("; ");

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  output: "standalone",
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "Content-Security-Policy", value: CSP },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
          {
            key: "Strict-Transport-Security",
            value: "max-age=63072000; includeSubDomains; preload",
          },
        ],
      },
    ];
  },
};

export default nextConfig;
