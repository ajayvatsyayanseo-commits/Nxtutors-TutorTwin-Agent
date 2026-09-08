import Link from "next/link";

import { DataTable, Empty, PageHeader, Pagination } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { formatDateTime, truncate } from "@/lib/format";
import type { AuditEntry, Page } from "@/lib/types";

export const metadata = { title: "Audit log · TutorTwin" };
export const dynamic = "force-dynamic";

/**
 * The audit log.
 *
 * Every high-risk row carries the before and after states, not merely the fact
 * that something changed — "someone changed the model route" leaves a reviewer
 * unable to say what it changed *from*, which is the question an incident
 * actually asks.
 *
 * Audit rows are written in the same transaction as the change they record, so
 * a change that committed always has its record.
 */
export default async function AuditPage({
  searchParams,
}: {
  searchParams: Promise<{
    action?: string;
    actor_id?: string;
    target_id?: string;
    high_risk_only?: string;
    since?: string;
    until?: string;
    page?: string;
  }>;
}) {
  const params = await searchParams;
  const page = Math.max(1, Number(params.page ?? 1) || 1);

  const data = await apiFetch<Page<AuditEntry>>("/v1/admin/audit", {
    query: {
      action: params.action,
      actor_id: params.actor_id,
      target_id: params.target_id,
      high_risk_only: params.high_risk_only === "yes",
      since: params.since ? `${params.since}T00:00:00Z` : undefined,
      until: params.until ? `${params.until}T23:59:59Z` : undefined,
      page,
      page_size: 25,
    },
  });

  const filtered = Boolean(
    params.action ||
      params.actor_id ||
      params.target_id ||
      params.high_risk_only ||
      params.since ||
      params.until,
  );

  return (
    <>
      <PageHeader
        title="Audit log"
        subtitle="Who did what, when, and why. Written in the same transaction as the change."
      />

      <form className="filters" method="get">
        <div className="field">
          <label htmlFor="action">Action</label>
          <input
            id="action"
            name="action"
            defaultValue={params.action ?? ""}
            placeholder="FEATURE_KILL_SWITCH"
          />
        </div>
        <div className="field">
          <label htmlFor="target_id">Target id</label>
          <input id="target_id" name="target_id" defaultValue={params.target_id ?? ""} />
        </div>
        <div className="field">
          <label htmlFor="since">From</label>
          <input id="since" name="since" type="date" defaultValue={params.since ?? ""} />
        </div>
        <div className="field">
          <label htmlFor="until">To</label>
          <input id="until" name="until" type="date" defaultValue={params.until ?? ""} />
        </div>
        <div className="field">
          <label htmlFor="high_risk_only" style={{ display: "flex", gap: 6 }}>
            <input
              id="high_risk_only"
              name="high_risk_only"
              type="checkbox"
              value="yes"
              defaultChecked={params.high_risk_only === "yes"}
            />
            <span>High-risk only</span>
          </label>
        </div>
        <button type="submit" className="primary">
          Apply
        </button>
        {filtered ? <Link href="/audit">Clear</Link> : null}
      </form>

      {data.items.length === 0 ? (
        <Empty
          title={filtered ? "No audit events match those filters" : "No audit events yet"}
        />
      ) : (
        <>
          <DataTable
            caption="Audit events"
            columns={[
              { key: "when", label: "When" },
              { key: "actor", label: "Actor" },
              { key: "action", label: "Action" },
              { key: "target", label: "Target" },
              { key: "reason", label: "Reason", wrap: true },
              { key: "change", label: "Before → after", wrap: true },
            ]}
          >
            {data.items.map((entry) => (
              <tr key={entry.id}>
                <td>{formatDateTime(entry.created_at)}</td>
                <td>
                  {entry.actor_email ?? entry.actor_id ?? entry.actor_type}
                  {entry.high_risk ? (
                    <span className="badge badge-bad" style={{ marginLeft: 6 }}>
                      high risk
                    </span>
                  ) : null}
                </td>
                <td className="mono">{entry.action}</td>
                <td className="mono">
                  {entry.target_type ? `${entry.target_type}:` : ""}
                  {entry.target_id ? truncate(entry.target_id, 40) : "—"}
                </td>
                <td className="wrap">{entry.reason ?? "—"}</td>
                <td className="wrap mono">
                  {entry.high_risk
                    ? `${JSON.stringify(entry.detail.before ?? {})} → ${JSON.stringify(
                        entry.detail.after ?? {},
                      )}`
                    : "—"}
                </td>
              </tr>
            ))}
          </DataTable>
          <Pagination
            page={data.page}
            pageSize={data.page_size}
            total={data.total}
            basePath="/audit"
            query={{
              action: params.action,
              actor_id: params.actor_id,
              target_id: params.target_id,
              high_risk_only: params.high_risk_only,
              since: params.since,
              until: params.until,
            }}
          />
        </>
      )}
    </>
  );
}
