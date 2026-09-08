import Link from "next/link";

import { DataTable, Empty, MoneyStat, PageHeader, ShareBar, Stat } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { formatMicros, formatNumber } from "@/lib/format";
import type { CostResponse } from "@/lib/types";

export const metadata = { title: "Costs · TutorTwin" };
export const dynamic = "force-dynamic";

const GROUPINGS = [
  "model",
  "provider",
  "capability",
  "student",
  "tutor",
  "media",
  "verification",
  "day",
] as const;
const WINDOWS = [7, 30, 90, 365];

/**
 * What each grouping actually means, shown under the filter.
 *
 * Three of these carry a caveat an operator would otherwise have to guess at,
 * and a cost report that is silently wrong about attribution is worse than one
 * that admits its edges.
 */
const GROUPING_NOTES: Record<string, string> = {
  model: "The model alias the call was routed to.",
  provider: "The vendor the call was billed by.",
  capability:
    "What the call was for, recorded when it happened. Calls made before this was recorded show as “unattributed”.",
  student: "The standalone identity the call was made for.",
  tutor:
    "Follows each student's current active tutor assignment — reassigning a student moves their past spend with them.",
  media: "Vision and transcription spend, separated from tutoring text.",
  verification: "Answer-checking spend, separated from answering.",
  day: "Calendar day, in the database's timezone.",
};

/**
 * Cost analysis.
 *
 * `group_by` is a closed literal on the API side, chosen from a fixed set of
 * column expressions — never interpolated into SQL. Asserted by
 * `test_cost_grouping_is_a_closed_set`.
 *
 * Totals are computed from the same `usage_ledger` rows the dashboard reads, at
 * the rate stored on each row when the call happened. A price change does not
 * rewrite last month's bill.
 */
export default async function CostsPage({
  searchParams,
}: {
  searchParams: Promise<{ window?: string; group_by?: string }>;
}) {
  const params = await searchParams;
  const windowDays = WINDOWS.includes(Number(params.window)) ? Number(params.window) : 30;
  const groupBy = (GROUPINGS as readonly string[]).includes(params.group_by ?? "")
    ? (params.group_by as string)
    : "model";

  const data = await apiFetch<CostResponse>("/v1/admin/costs", {
    query: { window_days: windowDays, group_by: groupBy },
  });

  const cachedShare =
    data.buckets.reduce((sum, bucket) => sum + bucket.input_tokens, 0) > 0
      ? `${Math.round(
          (100 * data.cached_tokens) /
            data.buckets.reduce((sum, bucket) => sum + bucket.input_tokens, 0),
        )}% of input tokens`
      : "no input tokens yet";

  return (
    <>
      <PageHeader
        title="Costs"
        subtitle={`Last ${windowDays} days, grouped by ${groupBy}. ${
          GROUPING_NOTES[groupBy] ?? ""
        } Rates are the ones stored at call time.`}
      />

      <form className="filters" method="get">
        <div className="field">
          <label htmlFor="window">Window</label>
          <select id="window" name="window" defaultValue={String(windowDays)}>
            {WINDOWS.map((value) => (
              <option key={value} value={value}>
                {value} days
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="group_by">Group by</label>
          <select id="group_by" name="group_by" defaultValue={groupBy}>
            {GROUPINGS.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </div>
        <button type="submit" className="primary">
          Apply
        </button>
        <Link href="/costs">Reset</Link>
      </form>

      <div className="grid">
        <MoneyStat label="Total spend" micros={data.total_cost_micros} />
        <Stat label="Model calls" value={data.total_calls} />
        <Stat
          label="Cached input tokens"
          value={data.cached_tokens}
          hint={cachedShare}
        />
      </div>

      {data.buckets.length === 0 ? (
        <Empty
          title="No spend in this window"
          hint="Deterministic paths — verification, twins, diagrams, grading — cost nothing and do not appear here."
        />
      ) : (
        <DataTable
          caption={`Cost by ${groupBy}`}
          columns={[
            { key: "key", label: groupBy },
            { key: "calls", label: "Calls", numeric: true },
            { key: "in", label: "Input tokens", numeric: true },
            { key: "out", label: "Output tokens", numeric: true },
            { key: "cached", label: "Cached", numeric: true },
            { key: "cost", label: "Cost", numeric: true },
            { key: "share", label: "Share", numeric: true },
            { key: "bar", label: "" },
          ]}
        >
          {data.buckets.map((bucket) => (
            <tr key={bucket.key}>
              <td className={groupBy === "student" ? "mono" : ""}>
                {groupBy === "student" && bucket.key !== "unattributed" ? (
                  <Link href={`/students/${bucket.key}`}>{bucket.label}</Link>
                ) : (
                  bucket.label
                )}
              </td>
              <td className="numeric">{formatNumber(bucket.calls)}</td>
              <td className="numeric">{formatNumber(bucket.input_tokens)}</td>
              <td className="numeric">{formatNumber(bucket.output_tokens)}</td>
              <td className="numeric">{formatNumber(bucket.cached_tokens)}</td>
              <td className="numeric">{formatMicros(bucket.cost_micros)}</td>
              <td className="numeric">
                {data.total_cost_micros > 0
                  ? `${Math.round((100 * bucket.cost_micros) / data.total_cost_micros)}%`
                  : "—"}
              </td>
              <td style={{ width: 180 }}>
                <ShareBar
                  share={
                    data.total_cost_micros > 0
                      ? bucket.cost_micros / data.total_cost_micros
                      : 0
                  }
                />
              </td>
            </tr>
          ))}
        </DataTable>
      )}
    </>
  );
}
