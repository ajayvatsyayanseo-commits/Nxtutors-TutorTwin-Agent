import Link from "next/link";

import { Badge, DataTable, Empty, PageHeader, Pagination } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import type { ConversationSummary, Page } from "@/lib/types";

export const metadata = { title: "Conversations · TutorTwin" };
export const dynamic = "force-dynamic";

export default async function ConversationsPage({
  searchParams,
}: {
  searchParams: Promise<{
    status?: string;
    student_id?: string;
    since?: string;
    until?: string;
    page?: string;
  }>;
}) {
  const params = await searchParams;
  const page = Math.max(1, Number(params.page ?? 1) || 1);

  const data = await apiFetch<Page<ConversationSummary>>("/v1/admin/conversations", {
    query: {
      status: params.status,
      student_id: params.student_id,
      // Dates are sent as-is; the API parses them and rejects what it cannot.
      since: params.since ? `${params.since}T00:00:00Z` : undefined,
      until: params.until ? `${params.until}T23:59:59Z` : undefined,
      page,
      page_size: 25,
    },
  });

  const filtered = Boolean(params.status || params.student_id || params.since || params.until);

  return (
    <>
      <PageHeader
        title="Conversations"
        subtitle="Every turn, with the requests, model calls and cost that produced it."
      />

      <form className="filters" method="get">
        <div className="field">
          <label htmlFor="status">Status</label>
          <select id="status" name="status" defaultValue={params.status ?? ""}>
            <option value="">Any</option>
            <option value="OPEN">Open</option>
            <option value="CLOSED">Closed</option>
          </select>
        </div>
        <div className="field">
          <label htmlFor="student_id">Student id</label>
          <input id="student_id" name="student_id" defaultValue={params.student_id ?? ""} />
        </div>
        <div className="field">
          <label htmlFor="since">Active since</label>
          <input id="since" name="since" type="date" defaultValue={params.since ?? ""} />
        </div>
        <div className="field">
          <label htmlFor="until">Active until</label>
          <input id="until" name="until" type="date" defaultValue={params.until ?? ""} />
        </div>
        <button type="submit" className="primary">
          Apply
        </button>
        {filtered ? <Link href="/conversations">Clear</Link> : null}
      </form>

      {data.items.length === 0 ? (
        <Empty
          title={filtered ? "No conversations match those filters" : "No conversations yet"}
        />
      ) : (
        <>
          <DataTable
            caption="Conversations"
            columns={[
              { key: "student", label: "Student" },
              { key: "tutor", label: "Tutor" },
              { key: "status", label: "Status" },
              { key: "source", label: "Source" },
              { key: "messages", label: "Messages", numeric: true },
              { key: "activity", label: "Last activity" },
            ]}
          >
            {data.items.map((row) => (
              <tr key={row.id}>
                <td>
                  <Link href={`/conversations/${row.id}`}>{row.student_identity}</Link>
                </td>
                <td>{row.tutor_name ?? "—"}</td>
                <td>
                  <Badge value={row.status} />
                </td>
                <td>{row.source}</td>
                <td className="numeric">{row.message_count}</td>
                <td>{formatDateTime(row.last_activity_at)}</td>
              </tr>
            ))}
          </DataTable>
          <Pagination
            page={data.page}
            pageSize={data.page_size}
            total={data.total}
            basePath="/conversations"
            query={{
              status: params.status,
              student_id: params.student_id,
              since: params.since,
              until: params.until,
            }}
          />
        </>
      )}
    </>
  );
}
