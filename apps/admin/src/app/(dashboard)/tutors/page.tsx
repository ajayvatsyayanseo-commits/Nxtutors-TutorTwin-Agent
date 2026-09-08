import Link from "next/link";

import { ActionForm } from "@/components/action-form";
import { Badge, DataTable, Empty, PageHeader } from "@/components/ui";
import { createTutorAction } from "@/lib/actions";
import { apiFetch } from "@/lib/api";
import { currentActor } from "@/lib/auth-actions";
import { formatDateTime } from "@/lib/format";
import { can } from "@/lib/session";
import type { TutorSummary } from "@/lib/types";

export const metadata = { title: "Tutors · TutorTwin" };
export const dynamic = "force-dynamic";

export default async function TutorsPage({
  searchParams,
}: {
  searchParams: Promise<{ q?: string }>;
}) {
  const params = await searchParams;
  const [tutors, actor] = await Promise.all([
    apiFetch<TutorSummary[]>("/v1/admin/tutors", { query: { q: params.q } }),
    currentActor(),
  ]);

  return (
    <>
      <PageHeader
        title="Tutors"
        subtitle="Each tutor has versioned personas. Only the active version reaches a student."
      />

      <form className="filters" method="get" role="search">
        <div className="field">
          <label htmlFor="q">Name contains</label>
          <input id="q" name="q" defaultValue={params.q ?? ""} />
        </div>
        <button type="submit" className="primary">
          Search
        </button>
        {params.q ? <Link href="/tutors">Clear</Link> : null}
      </form>

      {tutors.length === 0 ? (
        <Empty
          title={params.q ? "No tutors match" : "No tutors yet"}
          hint={params.q ? undefined : "Create one below, then draft a persona for it."}
        />
      ) : (
        <DataTable
          caption="Tutors"
          columns={[
            { key: "name", label: "Name" },
            { key: "status", label: "Status" },
            { key: "active", label: "Active persona", numeric: true },
            { key: "versions", label: "Versions", numeric: true },
            { key: "students", label: "Students", numeric: true },
            { key: "created", label: "Created" },
          ]}
        >
          {tutors.map((tutor) => (
            <tr key={tutor.id}>
              <td>
                <Link href={`/tutors/${tutor.id}`}>{tutor.display_name}</Link>
              </td>
              <td>
                <Badge value={tutor.status} />
              </td>
              <td className="numeric">
                {tutor.active_persona_version ?? (
                  <span className="badge badge-warn">none active</span>
                )}
              </td>
              <td className="numeric">{tutor.persona_versions}</td>
              <td className="numeric">{tutor.assigned_students}</td>
              <td>{formatDateTime(tutor.created_at)}</td>
            </tr>
          ))}
        </DataTable>
      )}

      {can(actor, "tutor:write") ? (
        <>
          <h3>Add a tutor</h3>
          <fieldset>
            <legend>New tutor</legend>
            <ActionForm action={createTutorAction} submitLabel="Create tutor">
              <div className="field">
                <label htmlFor="display_name">Display name</label>
                <input id="display_name" name="display_name" required maxLength={256} />
              </div>
            </ActionForm>
          </fieldset>
        </>
      ) : null}
    </>
  );
}
