import type { NextConfig } from "next";

/**
 * Security headers are set here rather than in a reverse proxy, so they travel
 * with the application to whatever host it is deployed on.
 *
 * The Content-Security-Policy is *not* here: it carries a per-request nonce and
 * is therefore built in `src/middleware.ts`. Everything below is the same on
 * every response, so it belongs in static config where it cannot be skipped by
 * a matcher.
 */
const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // Ships only the files the server actually imports. The container is a
  // fraction of the size, which is a fraction of the cold start and a much
  // smaller set of packages to keep patched.
  output: "standalone",
  experimental: {
    // Lets a server component answer a refused read with `forbidden()`, which
    // renders app/forbidden.tsx under a real 403. Without it the page an
    // operator may not open still returns 200 with an error panel inside, and a
    // monitor or a scripted client cannot tell refusal from success.
    authInterrupts: true,
  },
  // The admin bundle is never served from a CDN and never embedded, so nothing
  // here needs to be cacheable by an intermediary.
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "no-referrer" },
          {
            key: "Permissions-Policy",
            value: "camera=(), microphone=(), geolocation=(), payment=()",
          },
          {
            key: "Strict-Transport-Security",
            value: "max-age=63072000; includeSubDomains; preload",
          },
          { key: "Cache-Control", value: "no-store, max-age=0" },
        ],
      },
    ];
  },
};

export default nextConfig;
