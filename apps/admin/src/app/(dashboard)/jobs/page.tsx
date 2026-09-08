import Link from "next/link";

import { ActionForm } from "@/components/action-form";
import { Badge, DataTable, Empty, PageHeader, Pagination } from "@/components/ui";
import { cancelJobAction, retryJobAction } from "@/lib/actions";
import { apiFetch } from "@/lib/api";
import { currentActor } from "@/lib/auth-actions";
import { formatDateTime, truncate } from "@/lib/format";
import { can } from "@/lib/session";
import type { JobView, Page } from "@/lib/types";

export const metadata = { title: "Jobs · TutorTwin" };
export const dynamic = "force-dynamic";

const RETRYABLE = new Set(["FAILED", "FAILED_PERMANENT", "PENDING"]);
const CANCELLABLE = new Set(["PENDING", "FAILED"]);

export default async function JobsPage({
  searchParams,
}: {
  searchParams: Promise<{
    state?: string;
    job_type?: string;
    correlation_id?: string;
    page?: string;
  }>;
}) {
  const params = await searchParams;
  const page = Math.max(1, Number(params.page ?? 1) || 1);

  const [data, actor] = await Promise.all([
    apiFetch<Page<JobView>>("/v1/admin/jobs", {
      query: {
        state: params.state,
        job_type: params.job_type,
        correlation_id: params.correlation_id,
        page,
        page_size: 25,
      },
    }),
    currentActor(),
  ]);

  const writable = can(actor, "job:write");
  const filtered = Boolean(params.state || params.job_type || params.correlation_id);

  return (
    <>
      <PageHeader
        title="Jobs"
        subtitle="Retry re-arms a row for the worker; it never executes work on this request."
      />

      <form className="filters" method="get">
        <div className="field">
          <label htmlFor="state">State</label>
          <select id="state" name="state" defaultValue={params.state ?? ""}>
            <option value="">Any</option>
            {["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "FAILED_PERMANENT", "CANCELLED"].map(
              (state) => (
                <option key={state} value={state}>
                  {state}
                </option>
              ),
            )}
          </select>
        </div>
        <div className="field">
          <label htmlFor="job_type">Type</label>
          <input id="job_type" name="job_type" defaultValue={params.job_type ?? ""} />
        </div>
        <div className="field">
          <label htmlFor="correlation_id">Correlation id</label>
          <input
            id="correlation_id"
            name="correlation_id"
            defaultValue={params.correlation_id ?? ""}
          />
        </div>
        <button type="submit" className="primary">
          Apply
        </button>
        {filtered ? <Link href="/jobs">Clear</Link> : null}
      </form>

      {data.items.length === 0 ? (
        <Empty title={filtered ? "No jobs match those filters" : "No jobs queued"} />
      ) : (
        <>
          <DataTable
            caption="Jobs"
            columns={[
              { key: "type", label: "Type" },
              { key: "state", label: "State" },
              { key: "attempts", label: "Attempts", numeric: true },
              { key: "owner", label: "Owner" },
              { key: "error", label: "Last error", wrap: true },
              { key: "correlation", label: "Correlation" },
              { key: "updated", label: "Updated" },
              { key: "actions", label: "" },
            ]}
          >
            {data.items.map((job) => (
              <tr key={job.id}>
                <td>{job.job_type}</td>
                <td>
                  <Badge value={job.state} />
                </td>
                <td className="numeric">
                  {job.attempts}/{job.max_attempts}
                </td>
                <td>
                  {job.owner_subject_id ? (
                    <Link href={`/students/${job.owner_subject_id}`}>
                      {job.owner_identity ?? "student"}
                    </Link>
                  ) : (
                    "—"
                  )}
                </td>
                <td className="wrap">
                  {job.last_error ? truncate(job.last_error, 140) : "—"}
                </td>
                <td className="mono">{job.correlation_id ?? "—"}</td>
                <td>{formatDateTime(job.updated_at)}</td>
                <td>
                  {writable ? (
                    <div className="row-actions">
                      {RETRYABLE.has(job.state) ? (
                        <details style={{ margin: 0 }}>
                          <summary>Retry</summary>
                          <ActionForm action={retryJobAction} submitLabel="Retry job">
                            <input type="hidden" name="job_id" value={job.id} />
                            <div className="field">
                              <label htmlFor={`retry-reason-${job.id}`}>Reason</label>
                              <input
                                id={`retry-reason-${job.id}`}
                                name="reason"
                                required
                                maxLength={500}
                              />
                            </div>
                          </ActionForm>
                        </details>
                      ) : null}
                      {CANCELLABLE.has(job.state) ? (
                        <details style={{ margin: 0 }}>
                          <summary>Cancel</summary>
                          <ActionForm
                            action={cancelJobAction}
                            submitLabel="Cancel job"
                            danger
                          >
                            <input type="hidden" name="job_id" value={job.id} />
                            <div className="field">
                              <label htmlFor={`cancel-reason-${job.id}`}>Reason</label>
                              <input
                                id={`cancel-reason-${job.id}`}
                                name="reason"
                                required
                                maxLength={500}
                              />
                            </div>
                          </ActionForm>
                        </details>
                      ) : null}
                      {/* A RUNNING job offers neither: a worker is mid-flight and
                          does not know it was cancelled, so a "cancelled" row
                          would simply disagree with reality. */}
                      {!RETRYABLE.has(job.state) && !CANCELLABLE.has(job.state) ? (
                        <span className="badge">no safe action</span>
                      ) : null}
                    </div>
                  ) : null}
                </td>
              </tr>
            ))}
          </DataTable>
          <Pagination
            page={data.page}
            pageSize={data.page_size}
            total={data.total}
            basePath="/jobs"
            query={{
              state: params.state,
              job_type: params.job_type,
              correlation_id: params.correlation_id,
            }}
          />
        </>
      )}
    </>
  );
}
