import Link from "next/link";

import { Badge, DataTable, Empty, PageHeader, Pagination } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import type { Page, StudentSummary } from "@/lib/types";

export const metadata = { title: "Students · TutorTwin" };
export const dynamic = "force-dynamic";

/**
 * Search and filtering are **server-side**.
 *
 * The student table is unbounded, so a client-side filter would mean shipping
 * every row to the browser first. The query parameters go straight to the API,
 * which binds them into SQL — proved safe against wildcards and injection by
 * `test_hostile_search_terms_are_safe_and_do_not_widen_the_result`.
 */
export default async function StudentsPage({
  searchParams,
}: {
  searchParams: Promise<{ q?: string; plan_code?: string; status?: string; page?: string }>;
}) {
  const params = await searchParams;
  const page = Math.max(1, Number(params.page ?? 1) || 1);

  const data = await apiFetch<Page<StudentSummary>>("/v1/admin/students", {
    query: {
      q: params.q,
      plan_code: params.plan_code,
      status: params.status,
      page,
      page_size: 25,
    },
  });

  const filtered = Boolean(params.q || params.plan_code || params.status);

  return (
    <>
      <PageHeader
        title="Students"
        subtitle="Standalone identities. Phase 09 overlays the website's source of truth."
      />

      <form className="filters" method="get" role="search">
        <div className="field">
          <label htmlFor="q">Search identity or name</label>
          <input id="q" name="q" defaultValue={params.q ?? ""} placeholder="+9199…" />
        </div>
        <div className="field">
          <label htmlFor="plan_code">Plan</label>
          <input
            id="plan_code"
            name="plan_code"
            defaultValue={params.plan_code ?? ""}
            placeholder="PRO"
          />
        </div>
        <div className="field">
          <label htmlFor="status">Status</label>
          <select id="status" name="status" defaultValue={params.status ?? ""}>
            <option value="">Any</option>
            <option value="ACTIVE">Active</option>
            <option value="DISABLED">Disabled</option>
          </select>
        </div>
        <button type="submit" className="primary">
          Apply
        </button>
        {filtered ? <Link href="/students">Clear</Link> : null}
      </form>

      {data.items.length === 0 ? (
        <Empty
          title={filtered ? "No students match those filters" : "No students yet"}
          hint={
            filtered
              ? "Clear the filters to see everyone."
              : "A student is created the first time a message arrives for them."
          }
        />
      ) : (
        <>
          <DataTable
            caption="Students"
            columns={[
              { key: "identity", label: "Identity" },
              { key: "name", label: "Name" },
              { key: "plan", label: "Plan" },
              { key: "entitlement", label: "Entitlement" },
              { key: "tutor", label: "Tutor" },
              { key: "status", label: "Status" },
              { key: "created", label: "Created" },
            ]}
          >
            {data.items.map((student) => (
              <tr key={student.id}>
                <td>
                  <Link href={`/students/${student.id}`}>
                    {student.external_identity_value}
                  </Link>
                </td>
                <td>{student.display_name ?? "—"}</td>
                <td>{student.plan_code ?? "—"}</td>
                <td>
                  <Badge value={student.entitlement_status} />
                </td>
                <td>{student.tutor_name ?? "—"}</td>
                <td>
                  <Badge value={student.status} />
                </td>
                <td>{formatDateTime(student.created_at)}</td>
              </tr>
            ))}
          </DataTable>
          <Pagination
            page={data.page}
            pageSize={data.page_size}
            total={data.total}
            basePath="/students"
            query={{ q: params.q, plan_code: params.plan_code, status: params.status }}
          />
        </>
      )}
    </>
  );
}
