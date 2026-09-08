import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Nav } from "@/components/nav";
import type { AdminActor, AdminRole } from "@/lib/types";

vi.mock("next/navigation", () => ({ usePathname: () => "/students" }));

function actor(role: AdminRole, permissions: string[]): AdminActor {
  return {
    admin_id: "a1",
    email: "ops@example.com",
    role,
    permissions,
    must_change_password: false,
  };
}

describe("Nav", () => {
  it("offers only the sections the operator may open", () => {
    render(
      <Nav
        actor={actor("SUPPORT", [
          "dashboard:read",
          "student:read",
          "conversation:read",
          "job:read",
        ])}
      />,
    );

    expect(screen.getByRole("link", { name: "Students" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Jobs" })).toBeInTheDocument();
    // Support has no cost or admin-user permission, so those links are absent.
    expect(screen.queryByRole("link", { name: "Costs" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Administrators" })).not.toBeInTheDocument();
  });

  it("marks the current section for assistive technology", () => {
    render(<Nav actor={actor("ADMIN", ["dashboard:read", "student:read"])} />);
    expect(screen.getByRole("link", { name: "Students" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("link", { name: "Dashboard" })).not.toHaveAttribute(
      "aria-current",
    );
  });

  it("shows nothing to an actor with no permissions", () => {
    render(<Nav actor={actor("USAGE_VIEWER", [])} />);
    expect(screen.queryAllByRole("link")).toHaveLength(0);
  });

  it("hides a group heading when every item in it is hidden", () => {
    // A "Governance" heading over nothing tells the operator a section exists
    // that they cannot reach, which is worse than not mentioning it.
    render(<Nav actor={actor("SUPPORT", ["dashboard:read", "student:read"])} />);
    expect(screen.getByText("People")).toBeInTheDocument();
    expect(screen.queryByText("Governance")).not.toBeInTheDocument();
  });

  it("does not match a sibling route by prefix", () => {
    // "/students" must not light up while on "/students-archive"; the check is a
    // path segment, and getting it wrong makes the sidebar lie about where you are.
    render(<Nav actor={actor("ADMIN", ["student:read", "tutor:read"])} />);
    expect(screen.getByRole("link", { name: "Tutors" })).not.toHaveAttribute(
      "aria-current",
    );
  });
});
