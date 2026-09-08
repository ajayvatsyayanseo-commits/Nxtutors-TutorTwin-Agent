import { headers } from "next/headers";
import Link from "next/link";
import { forbidden, redirect } from "next/navigation";
import type { ReactNode } from "react";

import { Nav } from "@/components/nav";
import { currentActor, logoutAction } from "@/lib/auth-actions";
import { permissionForPath } from "@/lib/sections";
import { can } from "@/lib/session";

/**
 * The authenticated shell.
 *
 * The session check happens here, in a server component, before any child page
 * renders. An unauthenticated visitor is redirected rather than shown a shell
 * full of failing panels.
 *
 * An operator still carrying a bootstrap password is sent to change it. That
 * password was printed to a terminal, so treating it as a working credential
 * indefinitely is how a bootstrap secret becomes a production one — the API
 * enforces the same rule on its side.
 *
 * The section gate lives here too, for one reason: refusing on the shell means
 * the response is a real 403 before any HTML has been streamed. Refusing three
 * fetches deep, once the page has already started, can only produce a 200 with
 * an apology inside it — which a monitor, a script or a log cannot tell from
 * success. The API still checks every call; this is not the enforcement, it is
 * the enforcement arriving legibly.
 */
export default async function DashboardLayout({ children }: { children: ReactNode }) {
  const actor = await currentActor();
  if (!actor) redirect("/login");
  if (actor.must_change_password) redirect("/change-password");

  const pathname = (await headers()).get("x-pathname") ?? "/";
  const required = permissionForPath(pathname);
  if (required && !can(actor, required)) forbidden();

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            TT
          </span>
          <div>
            <h1>TutorTwin</h1>
            <p className="env">Control plane · {process.env.TUTORTWIN_ENVIRONMENT ?? "local"}</p>
          </div>
        </div>

        <div className="who">
          <span className="email">{actor.email}</span>
          <span className="badge badge-accent">{actor.role}</span>
        </div>

        <Nav actor={actor} />

        <div className="sidebar-footer">
          <form action={logoutAction}>
            <button type="submit" style={{ width: "100%" }}>
              Sign out
            </button>
          </form>
          <p style={{ fontSize: 11, margin: 0, textAlign: "center" }}>
            <Link href="/change-password">Change password</Link>
          </p>
        </div>
      </aside>
      <main id="main">{children}</main>
    </div>
  );
}
