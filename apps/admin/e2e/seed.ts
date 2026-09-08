/**
 * Seed the fixture data the end-to-end suite needs.
 *
 * Run once against a test database, with the API already up:
 *
 *     TUTORTWIN_API_URL=http://127.0.0.1:8000 \
 *     TUTORTWIN_INTERNAL_API_KEY=<the key the API is running with> \
 *     npx tsx e2e/seed.ts
 *
 * Everything is created **through the API**, using the bootstrap administrator,
 * so seeding exercises the same authorisation path an operator does. A seeder
 * that wrote to Postgres directly would still succeed if every endpoint were
 * broken, which would make the suite green for the wrong reason.
 *
 * The first administrator has to exist already:
 *
 *     python -m tutortwin.cli.admin bootstrap --email <email>
 */

import { API_URL, SUPER_ADMIN, SUPPORT_ADMIN, apiCall, apiLogin } from "./fixtures";

/** The plan the fixture student is put on. Local to the suite, never a real one. */
const PLAN_CODE = "E2E_PRO";

/** The identity every seeded turn arrives from. */
const FIXTURE_IDENTITY = "+919999000001";

/**
 * Post an inbound event, the way the messaging adapter does.
 *
 * A student exists because a message arrived; there is deliberately no admin
 * endpoint that invents one. This is the only place the suite uses the internal
 * key, and it is read from the environment rather than written down here.
 */
async function sendEvent(id: string, identity: string, text: string): Promise<void> {
  const key = process.env.TUTORTWIN_INTERNAL_API_KEY;
  if (!key) {
    throw new Error(
      "TUTORTWIN_INTERNAL_API_KEY is not set. The seeder needs it to post an inbound " +
        "event, which is the only way a student comes into existence.",
    );
  }

  const response = await fetch(`${API_URL}/v1/events`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-internal-key": key },
    body: JSON.stringify({
      event_id: id,
      request_id: `${id}-rq`,
      correlation_id: `${id}-co`,
      source: "e2e",
      subject: { external_type: "phone", external_id: identity },
      message: { message_id: id, type: "TEXT", text },
      occurred_at: new Date().toISOString(),
    }),
  });
  if (!response.ok) {
    throw new Error(`POST /v1/events -> ${response.status}: ${await response.text()}`);
  }
}

async function main(): Promise<void> {
  console.warn(`Seeding against ${API_URL}`);
  const session = await apiLogin(SUPER_ADMIN.email, SUPER_ADMIN.password);

  // A second, lower-privileged operator, so the RBAC scenario has a real account
  // rather than a mocked role.
  const admins = await apiCall<{ email: string }[]>(session, "/v1/admin/admins");
  if (!admins.some((admin) => admin.email === SUPPORT_ADMIN.email)) {
    // A newly created operator always lands with `must_change_password`, because
    // the password was typed by somebody else. The seeder walks that first-login
    // change rather than pretending it does not happen - otherwise every test
    // that signs in as this operator is redirected to the change-password page.
    const temporary = `${SUPPORT_ADMIN.password}-initial`;
    await apiCall(session, "/v1/admin/admins", {
      method: "POST",
      body: {
        email: SUPPORT_ADMIN.email,
        display_name: "E2E Support",
        role: "SUPPORT",
        password: temporary,
        reason: "end-to-end suite fixture operator",
        confirm: true,
      },
    });

    const firstLogin = await apiLogin(SUPPORT_ADMIN.email, temporary);
    await apiCall(firstLogin, "/v1/admin/auth/change-password", {
      method: "POST",
      body: { current_password: temporary, new_password: SUPPORT_ADMIN.password },
    });
    console.warn("  created support operator and completed its first password change");
  }

  const tutors = await apiCall<{ id: string; display_name: string }[]>(
    session,
    "/v1/admin/tutors",
  );
  if (tutors.length === 0) {
    await apiCall(session, "/v1/admin/tutors", {
      method: "POST",
      body: { display_name: "E2E Tutor" },
    });
    console.warn("  created tutor");
  }

  // A plan the fixture student can actually be on. Without one, every request is
  // refused at the entitlement gate and no conversation is ever created - which
  // is correct behaviour and useless as a fixture.
  const plans = await apiCall<{ plan_code: string }[]>(session, "/v1/admin/plans");
  if (!plans.some((plan) => plan.plan_code === PLAN_CODE)) {
    await apiCall(session, "/v1/admin/plans", {
      method: "POST",
      body: {
        plan_code: PLAN_CODE,
        allows_paid_ai: true,
        features: { pdf: true, image: true, voice: true, mock_tests: true },
        limits: { daily_call_limit: 500, user_daily_budget_micros: 5000000 },
        reason: "end-to-end suite fixture plan",
        confirm: true,
      },
    });
    console.warn(`  published plan ${PLAN_CODE}`);
  }

  // One message brings the student into existence; it is refused at the gate,
  // which is exactly what an unentitled student should get.
  await sendEvent(`seed-warmup-${Date.now()}`, FIXTURE_IDENTITY, "hello, are you there?");

  const students = await apiCall<{ total: number; items: { id: string }[] }>(
    session,
    `/v1/admin/students?q=${encodeURIComponent(FIXTURE_IDENTITY)}&page_size=1`,
  );
  const student = students.items[0];
  if (!student) {
    throw new Error("The warm-up event did not create a student. Check the API logs.");
  }

  await apiCall(session, `/v1/admin/students/${student.id}/entitlement`, {
    method: "POST",
    body: {
      plan_code: PLAN_CODE,
      status: "ACTIVE",
      reason: "end-to-end suite fixture entitlement",
      confirm: true,
    },
  });
  console.warn("  entitled the fixture student");

  // Now that the gate lets them through, two more turns produce a real
  // conversation with real request rows for the inspection scenario.
  const stamp = Date.now();
  await sendEvent(`seed-turn-a-${stamp}`, FIXTURE_IDENTITY, "explain photosynthesis step by step");
  await sendEvent(`seed-turn-b-${stamp}`, FIXTURE_IDENTITY, "why does that need light?");

  const conversations = await apiCall<{ total: number }>(
    session,
    `/v1/admin/conversations?student_id=${student.id}&page_size=1`,
  );
  console.warn(`  conversations for the fixture student: ${conversations.total}`);

  console.warn("Seed complete.");
}

main().catch((error: unknown) => {
  console.error(error);
  process.exitCode = 1;
});
