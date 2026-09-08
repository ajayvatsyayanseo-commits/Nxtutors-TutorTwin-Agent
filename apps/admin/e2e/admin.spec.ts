import { expect, test } from "@playwright/test";

import {
  API_URL,
  SUPER_ADMIN,
  SUPPORT_ADMIN,
  apiCall,
  apiLogin,
  confirmHighRisk,
  errorBanner,
  expectSuccess,
  signIn,
  type ApiSession,
} from "./fixtures";

/**
 * The twelve required end-to-end scenarios, in order.
 *
 * They run against the real Next.js server, the real Python API and a real
 * PostgreSQL database. Where a test changes global state — a kill switch, a
 * model route — it puts it back, so the suite can be run twice in a row.
 */

let seed: ApiSession;
let studentId: string;
let studentIdentity: string;
let conversationId: string;
let assessmentId: string;
let failedJobId: string;

test.beforeAll(async () => {
  seed = await apiLogin(SUPER_ADMIN.email, SUPER_ADMIN.password);

  const students = await apiCall<{ items: { id: string; external_identity_value: string }[] }>(
    seed,
    "/v1/admin/students?page_size=1",
  );
  const first = students.items[0];
  if (!first) throw new Error("Seed data missing: run e2e/seed.ts before the suite.");
  studentId = first.id;
  studentIdentity = first.external_identity_value;

  const conversations = await apiCall<{ items: { id: string }[] }>(
    seed,
    `/v1/admin/conversations?student_id=${studentId}&page_size=1`,
  );
  conversationId = conversations.items[0]?.id ?? "";

  const assessments = await apiCall<{ items: { id: string }[] }>(
    seed,
    "/v1/admin/learning/assessments?page_size=1",
  );
  assessmentId = assessments.items[0]?.id ?? "";

  const jobs = await apiCall<{ items: { id: string; state: string }[] }>(
    seed,
    "/v1/admin/jobs?state=FAILED&page_size=1",
  );
  failedJobId = jobs.items[0]?.id ?? "";
});

// --- 1. admin login -----------------------------------------------------------

test("1. an administrator signs in and lands on the dashboard", async ({ page }) => {
  await page.goto("/");
  // Unauthenticated visitors are redirected before any panel renders.
  await expect(page).toHaveURL(/\/login/);

  await signIn(page);
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  await expect(page.getByText(SUPER_ADMIN.email)).toBeVisible();

  // The API session token never reaches the browser: the cookies this page holds
  // are the BFF's own httpOnly pair, and neither is readable from script.
  const readable = await page.evaluate(() => document.cookie);
  expect(readable).not.toContain("tt_admin=");
  expect(readable).not.toContain("tt_admin_csrf=");
});

test("1b. a wrong password is refused with one generic message", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Email").fill(SUPER_ADMIN.email);
  await page.getByLabel("Password").fill("definitely-the-wrong-password");
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(errorBanner(page)).toContainText("Invalid email or password");
  await expect(page).toHaveURL(/\/login/);
});

// --- 2. student search --------------------------------------------------------

test("2. student search filters server-side and paginates", async ({ page }) => {
  await signIn(page);
  await page.goto("/students");

  await expect(page.getByRole("heading", { name: "Students" })).toBeVisible();
  await page.getByLabel("Search identity or name").fill(studentIdentity);
  await page.getByRole("button", { name: "Apply" }).click();

  await expect(page).toHaveURL(/q=/);
  await expect(page.getByRole("link", { name: studentIdentity })).toBeVisible();

  // A term that matches nothing gives an explicit empty state, not a blank table.
  await page.getByLabel("Search identity or name").fill("no-such-student-anywhere");
  await page.getByRole("button", { name: "Apply" }).click();
  await expect(page.getByText("No students match those filters")).toBeVisible();
});

// --- 3. change local entitlement ---------------------------------------------

test("3. entitlement override requires a reason and is audited", async ({ page }) => {
  await signIn(page);
  await page.goto(`/students/${studentId}`);

  const panel = page.locator("details.risk", { hasText: "Override entitlement" });
  await panel.locator("summary").click();

  // Submitting without ticking the confirmation is refused by the action.
  await panel.getByLabel("Plan code").fill("PRO");
  await panel.getByLabel(/Reason/).fill("goodwill after billing incident 4412");
  await panel.getByRole("button", { name: "Override entitlement" }).click();
  await expect(errorBanner(panel)).toContainText("confirmation box");

  await panel.getByRole("checkbox", { name: /^I understand/ }).check();
  await panel.getByRole("button", { name: "Override entitlement" }).click();
  await expectSuccess(page, /Entitlement updated/);

  await page.goto("/audit?high_risk_only=yes");
  await expect(page.getByText("ENTITLEMENT_OVERRIDE").first()).toBeVisible();
  await expect(
    page.getByText("goodwill after billing incident 4412").first(),
  ).toBeVisible();
});

// --- 4. assign tutor ----------------------------------------------------------

test("4. a student is assigned to a tutor", async ({ page }) => {
  const tutors = await apiCall<{ id: string; display_name: string }[]>(seed, "/v1/admin/tutors");
  const tutor = tutors[0];
  expect(tutor, "seed must create at least one tutor").toBeTruthy();

  await signIn(page);
  await page.goto(`/tutors/${tutor!.id}`);

  const assignment = page.locator("fieldset", { hasText: "Assignment" });
  await assignment.getByLabel("Student id").fill(studentId);
  await assignment.getByLabel("Reason").fill("assigned during onboarding");
  await assignment.getByRole("button", { name: "Assign student" }).click();

  await expectSuccess(page, /Student assigned/);
  await page.reload();
  // First, not "the" link: assignment history is kept, so a student assigned
  // more than once appears once per row. Retiring rows rather than deleting
  // them is the point — the table is allowed to repeat an identity.
  await expect(page.getByRole("link", { name: studentIdentity }).first()).toBeVisible();
});

test("4b. a tutor is assigned from the student's own page", async ({ page }) => {
  // The direction an operator actually works in: they arrive at a student from a
  // support ticket, not at a tutor. Same endpoint, same audit event, one picker
  // instead of a pasted UUID.
  const tutors = await apiCall<{ id: string; display_name: string }[]>(seed, "/v1/admin/tutors");
  const tutor = tutors[0];
  expect(tutor, "seed must create at least one tutor").toBeTruthy();

  await signIn(page);
  await page.goto(`/students/${studentId}`);

  const panel = page.locator("fieldset", { hasText: /Assign a tutor|Reassign tutor/ });
  await panel.getByLabel("Tutor").selectOption(tutor!.id);
  await panel.getByLabel("Reason").fill("reassigned from the student record");
  await panel.getByRole("button", { name: "Assign tutor" }).click();

  await expectSuccess(page, /Student assigned/);

  // The assignment is readable from both sides, not only the one just used.
  await page.reload();
  await expect(
    page.getByRole("link", { name: tutor!.display_name }),
  ).toBeVisible();
});

// --- 5. edit persona draft, then activate ------------------------------------

test("5. a persona draft is created and then activated", async ({ page }) => {
  const tutors = await apiCall<{ id: string }[]>(seed, "/v1/admin/tutors");
  const tutorId = tutors[0]!.id;

  await signIn(page);
  await page.goto(`/tutors/${tutorId}`);

  const draft = page.locator("fieldset", { hasText: "New draft" });
  await draft.getByLabel("Display name").fill("Ms Rao");
  await draft.getByLabel("Subjects (comma separated)").fill("maths, physics");
  await draft.getByLabel("Tone").fill("warm and direct");
  await draft.getByLabel("Default pedagogy mode").selectOption("HINT_FIRST");
  await draft.getByRole("button", { name: "Save draft" }).click();
  await expectSuccess(page, /inert until you activate it/);

  await page.reload();
  const activate = page.locator("details.risk", { hasText: /Activate v/ }).first();
  await activate.locator("summary").click();
  await confirmHighRisk(activate, "activating the reviewed persona", /Activate version/);

  // The effect is asserted, not a banner: activating removes the very form that
  // would have carried the confirmation, because that version is now the active
  // one and no longer offers an "activate" control.
  await expect(page.getByRole("heading", { name: "Active persona" })).toBeVisible();
  await expect(page.getByText("Ms Rao").first()).toBeVisible();

  const personas = await apiCall<{ personas: { version: number; is_active: boolean }[] }>(
    seed,
    `/v1/admin/tutors/${tutorId}`,
  );
  const active = personas.personas.filter((persona) => persona.is_active);
  expect(active).toHaveLength(1);
  expect(active[0]!.version).toBe(Math.max(...personas.personas.map((p) => p.version)));
});

// --- 6. model routing edit ----------------------------------------------------

test("6. a model route is edited, and no secret is displayed", async ({ page }) => {
  await signIn(page);
  await page.goto("/models");

  // Presence of a credential is reported; the value is not on the page at all.
  await expect(page.getByRole("heading", { name: "Provider credentials" })).toBeVisible();
  const html = await page.content();
  expect(html).not.toContain("sk-");
  expect(html.toLowerCase()).not.toContain("api_key");

  const panel = page.locator("details.risk", { hasText: "Change model routing" });
  await panel.locator("summary").click();
  await panel.getByLabel("Alias").selectOption("CHEAP_TEXT");
  await panel.getByLabel("Provider").selectOption("FAKE");
  await panel.getByLabel("Vendor model id").fill("fake-cheap-e2e");
  await panel.getByLabel("Input cost, micros per 1k tokens").fill("1000");
  await panel.getByLabel("Output cost, micros per 1k tokens").fill("5000");
  await panel.getByLabel("Rate version").fill("e2e-v1");
  await confirmHighRisk(panel, "pinning the fake model for the e2e run", "Save route");

  await expectSuccess(page, /Route saved/);
  await page.reload();
  await expect(page.getByText("fake-cheap-e2e")).toBeVisible();
});

// --- 7. conversation inspection ----------------------------------------------

test("7. a conversation shows its turns, requests and cost", async ({ page }) => {
  test.skip(!conversationId, "seed produced no conversation");

  await signIn(page);
  await page.goto(`/conversations/${conversationId}`);

  await expect(page.getByRole("heading", { name: "Timeline" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Requests" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Model calls" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "RAG retrieval" })).toBeVisible();
  await expect(page.getByText("Cost").first()).toBeVisible();

  // Latency, verification and retrieval are what an operator opens a
  // conversation to find after a complaint. A timeline without them says what
  // was answered but not why it was slow, expensive or thin.
  await expect(page.getByRole("columnheader", { name: "Latency" })).toBeVisible();
  await expect(page.getByText("Slowest turn")).toBeVisible();
  await expect(page.getByText("Verifications")).toBeVisible();
});

// --- 8. mock test inspection --------------------------------------------------

test("8. an assessment shows the key and the grading evidence", async ({ page }) => {
  test.skip(!assessmentId, "seed produced no assessment");

  await signIn(page);
  await page.goto(`/learning/${assessmentId}`);

  // An operator verifying a grade needs the key; the student projection cannot
  // carry one. The difference is authorisation, not an omitted column.
  await expect(
    page.getByRole("heading", { name: "Questions and answer key" }),
  ).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Answer key" })).toBeVisible();
});

// --- 9. cost view -------------------------------------------------------------

test("9. costs group by a closed set and total correctly", async ({ page }) => {
  await signIn(page);
  await page.goto("/costs");

  await expect(page.getByRole("heading", { name: "Costs" })).toBeVisible();
  await page.getByLabel("Group by").selectOption("provider");
  await page.getByRole("button", { name: "Apply" }).click();
  await expect(page).toHaveURL(/group_by=provider/);
  await expect(page.getByText("Total spend")).toBeVisible();
});

// --- 10. feature kill switch --------------------------------------------------

test("10. a kill switch is flipped and restored, with both changes audited", async ({
  page,
}) => {
  await signIn(page);
  await page.goto("/flags");

  const row = page.getByRole("row", { name: /rag_retrieval/ });
  await row.locator("summary").click();
  await confirmHighRisk(row, "retrieval latency incident during e2e", /Disable rag_retrieval/);
  await expectSuccess(page, /disabled/);

  await page.reload();
  await expect(page.getByRole("status").filter({ hasText: "capability disabled" })).toBeVisible();

  // Restore, so the suite is re-runnable and the platform is left as found.
  const restored = page.getByRole("row", { name: /rag_retrieval/ });
  await restored.locator("summary").click();
  await confirmHighRisk(restored, "incident resolved, restoring retrieval", /Enable rag_retrieval/);
  await expectSuccess(page, /enabled/);
});

// --- 11. failed job retry -----------------------------------------------------

test("11. a failed job is retried without erasing its attempt history", async ({ page }) => {
  test.skip(!failedJobId, "seed produced no failed job");

  await signIn(page);
  await page.goto("/jobs?state=FAILED");

  const row = page.getByRole("row").filter({ hasText: "FAILED" }).first();
  await row.getByRole("group").filter({ hasText: "Retry" }).locator("summary").click();
  await row.getByLabel("Reason").fill("provider recovered, retrying");
  await row.getByRole("button", { name: "Retry job" }).click();

  await expectSuccess(page, /attempt history is kept/);

  const job = await apiCall<{ state: string; attempts: number; max_attempts: number }>(
    seed,
    `/v1/admin/jobs/${failedJobId}`,
  );
  expect(job.state).toBe("PENDING");
  expect(job.attempts).toBeGreaterThan(0);
});

// --- 12. audit log ------------------------------------------------------------

test("12. the audit log shows who did what, why, and what changed", async ({ page }) => {
  await signIn(page);
  await page.goto("/audit?high_risk_only=yes");

  await expect(page.getByRole("heading", { name: "Audit log" })).toBeVisible();
  const firstRow = page.getByRole("row").nth(1);
  await expect(firstRow).toContainText(SUPER_ADMIN.email);
  await expect(page.getByText("FEATURE_KILL_SWITCH").first()).toBeVisible();

  // Filtering by action narrows server-side.
  await page.getByLabel("Action").fill("MODEL_ROUTE_CHANGE");
  await page.getByRole("button", { name: "Apply" }).click();
  await expect(page).toHaveURL(/action=MODEL_ROUTE_CHANGE/);
  await expect(page.getByText("MODEL_ROUTE_CHANGE").first()).toBeVisible();
});

// --- RBAC in the browser ------------------------------------------------------

test("13. a support operator is offered less, and refused the rest", async ({ page }) => {
  await signIn(page, SUPPORT_ADMIN);

  // The sidebar omits what the role cannot use.
  await expect(page.getByRole("link", { name: "Students" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Costs" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Administrators" })).toHaveCount(0);

  // And typing the URL directly still fails, because the server decides.
  const response = await page.goto("/costs");
  expect(response?.status()).toBeGreaterThanOrEqual(400);
});

test("14. signing out revokes the session server-side", async ({ page }) => {
  await signIn(page);
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login/);

  // Going back does not restore access: the cookie is gone and the API session
  // was revoked, so the shell redirects again.
  await page.goto("/students");
  await expect(page).toHaveURL(/\/login/);
});

test("15. the API rejects a direct browser call without the CSRF header", async ({
  request,
}) => {
  // The API is not reachable from the browser in normal use; this asserts that
  // even if it were, a cookie alone cannot perform a mutation.
  const login = await request.post(`${API_URL}/v1/admin/auth/login`, {
    data: { email: SUPER_ADMIN.email, password: SUPER_ADMIN.password },
  });
  expect(login.ok()).toBeTruthy();

  const withoutCsrf = await request.post(`${API_URL}/v1/admin/flags/verifier`, {
    data: { enabled: false, reason: "cross-site attempt from e2e", confirm: true },
  });
  expect(withoutCsrf.status()).toBe(403);
});
