import Link from "next/link";

import { Badge, DataTable, Empty, PageHeader, Pagination } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { formatDateTime, formatNumber, shortId } from "@/lib/format";
import type { DocumentSummary, Page } from "@/lib/types";

export const metadata = { title: "Documents & RAG · TutorTwin" };
export const dynamic = "force-dynamic";

const VISIBILITIES = [
  "GLOBAL_CURATED",
  "TUTOR",
  "COURSE",
  "STUDENT_PRIVATE",
  "CONVERSATION",
];

export default async function DocumentsPage({
  searchParams,
}: {
  searchParams: Promise<{
    q?: string;
    visibility?: string;
    status?: string;
    student_id?: string;
    include_deleted?: string;
    page?: string;
  }>;
}) {
  const params = await searchParams;
  const page = Math.max(1, Number(params.page ?? 1) || 1);

  const data = await apiFetch<Page<DocumentSummary>>("/v1/admin/documents", {
    query: {
      q: params.q,
      visibility: params.visibility,
      status: params.status,
      student_id: params.student_id,
      include_deleted: params.include_deleted === "yes",
      page,
      page_size: 25,
    },
  });

  const filtered = Boolean(
    params.q || params.visibility || params.status || params.student_id,
  );

  return (
    <>
      <PageHeader
        title="Documents & RAG"
        subtitle="Ingested sources and their chunks. Vectors are never displayed — a 1536-float array is not diagnostic."
      />

      <form className="filters" method="get" role="search">
        <div className="field">
          <label htmlFor="q">Title contains</label>
          <input id="q" name="q" defaultValue={params.q ?? ""} />
        </div>
        <div className="field">
          <label htmlFor="visibility">Visibility</label>
          <select id="visibility" name="visibility" defaultValue={params.visibility ?? ""}>
            <option value="">Any</option>
            {VISIBILITIES.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="status">Status</label>
          <select id="status" name="status" defaultValue={params.status ?? ""}>
            <option value="">Any</option>
            <option value="PENDING">PENDING</option>
            <option value="READY">READY</option>
            <option value="FAILED">FAILED</option>
          </select>
        </div>
        <div className="field">
          <label htmlFor="include_deleted" style={{ display: "flex", gap: 6 }}>
            <input
              id="include_deleted"
              name="include_deleted"
              type="checkbox"
              value="yes"
              defaultChecked={params.include_deleted === "yes"}
            />
            <span>Include deleted</span>
          </label>
        </div>
        <button type="submit" className="primary">
          Apply
        </button>
        {filtered ? <Link href="/documents">Clear</Link> : null}
      </form>

      {data.items.length === 0 ? (
        <Empty
          title={filtered ? "No documents match those filters" : "Nothing ingested yet"}
        />
      ) : (
        <>
          <DataTable
            caption="Knowledge sources"
            columns={[
              { key: "title", label: "Title", wrap: true },
              { key: "owner", label: "Owner" },
              { key: "visibility", label: "Visibility" },
              { key: "status", label: "Status" },
              { key: "chunks", label: "Chunks", numeric: true },
              { key: "hash", label: "Content hash" },
              { key: "created", label: "Ingested" },
            ]}
          >
            {data.items.map((row) => (
              <tr key={row.id}>
                <td className="wrap">
                  <Link href={`/documents/${row.id}`}>{row.title}</Link>
                  {row.deleted_at ? (
                    <span className="badge badge-bad" style={{ marginLeft: 6 }}>
                      deleted
                    </span>
                  ) : null}
                </td>
                <td>
                  {row.owner_subject_id ? (
                    <Link href={`/students/${row.owner_subject_id}`}>
                      {row.owner_identity ?? shortId(row.owner_subject_id)}
                    </Link>
                  ) : (
                    "—"
                  )}
                </td>
                <td>
                  <Badge value={row.visibility} />
                </td>
                <td>
                  <Badge value={row.status} />
                </td>
                <td className="numeric">{formatNumber(row.chunk_count)}</td>
                <td className="mono">{shortId(row.content_sha256)}</td>
                <td>{formatDateTime(row.created_at)}</td>
              </tr>
            ))}
          </DataTable>
          <Pagination
            page={data.page}
            pageSize={data.page_size}
            total={data.total}
            basePath="/documents"
            query={{
              q: params.q,
              visibility: params.visibility,
              status: params.status,
              student_id: params.student_id,
              include_deleted: params.include_deleted,
            }}
          />
        </>
      )}
    </>
  );
}
