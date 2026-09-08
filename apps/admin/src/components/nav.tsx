"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { SECTION_GROUPS, isCurrent } from "@/lib/sections";
import type { AdminActor } from "@/lib/types";

/**
 * Sidebar navigation, filtered by permission.
 *
 * **Hiding a link is a courtesy, not a control.** The API refuses the request
 * regardless — proved by `test_role_matrix_is_enforced_by_the_server` — and the
 * authenticated layout refuses the page from the same list this renders from,
 * so a link that is hidden here is also a 403 if its URL is typed by hand.
 *
 * A client component solely because the active link needs the current path.
 * It receives the actor as a prop; it never reads a token.
 *
 * The sections are grouped by the question being asked — who is using this, what
 * is it configured to do, what did it cost, who changed it — because fourteen
 * flat links is a list an operator reads rather than a menu they aim at. A group
 * whose every item is hidden by permission does not render its heading either.
 */
export function Nav({ actor }: { actor: AdminActor }) {
  const pathname = usePathname();

  return (
    <nav aria-label="Control plane sections">
      {SECTION_GROUPS.map((group) => {
        const visible = group.items.filter((item) =>
          actor.permissions.includes(item.permission),
        );
        if (visible.length === 0) return null;

        return (
          <div key={group.title}>
            <p className="nav-group">{group.title}</p>
            {visible.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                aria-current={isCurrent(pathname, item.href) ? "page" : undefined}
              >
                <span className="nav-dot" aria-hidden="true" />
                {item.label}
              </Link>
            ))}
          </div>
        );
      })}
    </nav>
  );
}
