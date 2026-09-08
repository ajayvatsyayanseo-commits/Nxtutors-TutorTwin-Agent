import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end tests against the real stack: this Next.js app, the real Python
 * API, and a real PostgreSQL database.
 *
 * Nothing is mocked. A control-plane test that stubs the API proves the buttons
 * render, not that an operator can actually change a plan — and the second is
 * the claim being made.
 *
 * The API must already be running on `TUTORTWIN_API_URL`; `e2e/seed.ts` creates
 * the fixture operator and data through it. Playwright starts only the Next.js
 * server, because starting the Python service from here would hide a
 * configuration failure behind a test harness.
 */
const BASE_URL = process.env.ADMIN_BASE_URL ?? "http://127.0.0.1:3100";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  // Serial by default: these tests share one database and several of them flip
  // global switches. Parallel runs would interfere in ways that look like flakes.
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  timeout: 60_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "npm run start",
    url: `${BASE_URL}/login`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: {
      TUTORTWIN_API_URL: process.env.TUTORTWIN_API_URL ?? "http://127.0.0.1:8000",
      NODE_ENV: "production",
      // `next start` reads PORT, so the suite can be pointed at a spare port and
      // run beside a development server instead of colliding with it.
      PORT: String(new URL(BASE_URL).port || 3100),
    },
  },
});
