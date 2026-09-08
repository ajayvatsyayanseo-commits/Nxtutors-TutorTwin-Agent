import { describe, expect, it, vi } from "vitest";

import type { AdminActor } from "@/lib/types";

// `lib/session` imports `server-only`, which throws when loaded outside a server
// component. The module under test here is the pure permission logic, so the
// marker is stubbed rather than the logic being moved somewhere weaker.
vi.mock("server-only", () => ({}));
vi.mock("next/headers", () => ({ cookies: vi.fn() }));

const { can, canAny } = await import("@/lib/session");

function actor(permissions: string[]): AdminActor {
  return {
    admin_id: "a1",
    email: "ops@example.com",
    role: "SUPPORT",
    permissions,
    must_change_password: false,
  };
}

describe("permission helpers", () => {
  it("grants only what the actor holds", () => {
    const support = actor(["student:read", "job:write"]);
    expect(can(support, "student:read")).toBe(true);
    expect(can(support, "student:write")).toBe(false);
  });

  it("treats a missing actor as holding nothing", () => {
    // A logged-out render must not accidentally show a control. `null` is the
    // shape the layout passes before it redirects.
    expect(can(null, "student:read")).toBe(false);
    expect(canAny(null, "student:read", "cost:read")).toBe(false);
  });

  it("canAny needs one of several", () => {
    const viewer = actor(["cost:read"]);
    expect(canAny(viewer, "student:write", "cost:read")).toBe(true);
    expect(canAny(viewer, "student:write", "flag:write")).toBe(false);
  });

  it("does not treat a permission prefix as a match", () => {
    // "student:read" must not satisfy "student:readwrite" or vice versa; the
    // check is equality on a full string, not a prefix.
    const reader = actor(["student:read"]);
    expect(can(reader, "student:readwrite")).toBe(false);
    expect(can(reader, "student")).toBe(false);
  });
});
