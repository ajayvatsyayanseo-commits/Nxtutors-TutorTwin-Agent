/**
 * The control plane's sections, and the permission each one needs.
 *
 * One list, two readers: the sidebar renders from it, and the authenticated
 * layout refuses from it. Keeping them apart is how a link ends up offered to
 * someone the page then refuses — or worse, how a page stops being gated when
 * a link is renamed.
 *
 * This is **not** the enforcement. The TutorTwin API checks the same permission
 * on every call and would refuse regardless; this list exists so the refusal
 * happens on the shell, with a real 403, instead of arriving as a broken panel
 * three fetches deep.
 */

export interface Section {
  href: string;
  label: string;
  permission: string;
}

export interface SectionGroup {
  title: string;
  items: Section[];
}

export const SECTION_GROUPS: SectionGroup[] = [
  {
    title: "Overview",
    items: [{ href: "/", label: "Dashboard", permission: "dashboard:read" }],
  },
  {
    title: "People",
    items: [
      { href: "/students", label: "Students", permission: "student:read" },
      { href: "/tutors", label: "Tutors", permission: "tutor:read" },
      {
        href: "/subscriptions",
        label: "Grant subscription",
        // student:write, not student:read - this page only ever grants access,
        // so an operator who cannot grant should not be offered the link.
        permission: "student:write",
      },
    ],
  },
  {
    title: "Activity",
    items: [
      { href: "/conversations", label: "Conversations", permission: "conversation:read" },
      { href: "/documents", label: "Documents & RAG", permission: "document:read" },
      { href: "/learning", label: "Learning", permission: "learning:read" },
      { href: "/jobs", label: "Jobs", permission: "job:read" },
    ],
  },
  {
    title: "Configuration",
    items: [
      { href: "/plans", label: "Plans", permission: "plan:read" },
      { href: "/models", label: "Models & routing", permission: "model:read" },
      { href: "/prompts", label: "Prompt versions", permission: "prompt:read" },
      { href: "/flags", label: "Feature flags", permission: "flag:read" },
    ],
  },
  {
    title: "Governance",
    items: [
      { href: "/costs", label: "Costs", permission: "cost:read" },
      { href: "/audit", label: "Audit log", permission: "audit:read" },
      { href: "/admins", label: "Administrators", permission: "admin_user:read" },
    ],
  },
];

export const SECTIONS: Section[] = SECTION_GROUPS.flatMap((group) => group.items);

/** Segment match, not prefix: "/students" must not light up on "/students-archive". */
export function isCurrent(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

/**
 * The permission a path needs, or null for a path outside the sections
 * (`/change-password`, which every signed-in operator may open).
 *
 * The longest matching section wins, so a future "/students/archive" section
 * would gate its own subtree rather than inheriting "/students".
 */
export function permissionForPath(pathname: string): string | null {
  let best: Section | null = null;
  for (const section of SECTIONS) {
    if (!isCurrent(pathname, section.href)) continue;
    if (!best || section.href.length > best.href.length) best = section;
  }
  return best?.permission ?? null;
}
