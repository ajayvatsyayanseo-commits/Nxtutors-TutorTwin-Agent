import Link from "next/link";

import {
  DataTable,
  Empty,
  MoneyStat,
  PageHeader,
  Segmented,
  ShareBar,
  Stat,
  type Tone,
} from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { formatDateTime, formatMicros, formatNumber } from "@/lib/format";
import type { Dashboard, HealthSummary } from "@/lib/types";

export const metadata = { title: "Dashboard · TutorTwin" };
export const dynamic = "force-dynamic";

const WINDOWS = [1, 7, 30, 90];

/** A percentage of a whole, or a phrase saying the denominator was zero. */
function share(part: number, whole: number, suffix: string, ifEmpty: string): string {
  if (whole <= 0) return ifEmpty;
  return `${Math.round((100 * part) / whole)}% ${suffix}`;
}

/** Latency reads as a duration, not as a count: 840 ms, then 3.2 s. */
function formatMs(ms: number | null): string {
  if (ms === null) return "—";
  return ms < 1000 ? `${formatNumber(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export default async function DashboardPage({
  searchParams,
}: {
  searchParams: Promise<{ window?: string }>;
}) {
  const params = await searchParams;
  const windowDays = WINDOWS.includes(Number(params.window)) ? Number(params.window) : 7;

  // Two independent reads, issued together: the dashboard aggregates a window
  // while the health summary is point-in-time, and neither needs the other.
  const [data, health] = await Promise.all([
    apiFetch<Dashboard>("/v1/admin/dashboard", { query: { window_days: windowDays } }),
    apiFetch<HealthSummary>("/v1/admin/health-summary"),
  ]);

  const { counts } = data;
  const totalExtractions = counts.local_extractions + counts.vision_escalations;
  const failureRate = counts.requests > 0 ? counts.requests_failed / counts.requests : 0;
  const cacheRate = counts.input_tokens > 0 ? counts.cached_input_tokens / counts.input_tokens : 0;

  // The tone is derived from the number, never hand-set per deployment: a
  // threshold written into the page is a threshold nobody remembers to revisit,
  // but one that is invisible is worse — these are stated here, in one place.
  const failureTone: Tone | undefined =
    counts.requests === 0 ? undefined : failureRate >= 0.1 ? "bad" : failureRate > 0 ? "warn" : "ok";
  const jobsTone: Tone | undefined = health.jobs_failed > 0 ? "bad" : undefined;
  const gradeTone: Tone | undefined = health.attempts_awaiting_grade > 0 ? "warn" : undefined;

  const maxAliasCost = Math.max(1, ...data.cost_by_alias.map((row) => row.cost_micros));
  const maxFailures = Math.max(1, ...data.failures_by_code.map((row) => row.count));

  return (
    <>
      <PageHeader
        title="Dashboard"
        subtitle={`Standalone TutorTwin activity for the last ${windowDays} day${
          windowDays === 1 ? "" : "s"
        }. Generated ${formatDateTime(data.generated_at)}.`}
        actions={
          <Segmented
            label="Time window"
            current={windowDays}
            options={WINDOWS.map((value) => ({ value, label: `${value}d` }))}
            href={(value) => `/?window=${value}`}
          />
        }
      />

      {health.jobs_failed > 0 ? (
        <div className="alert alert-warning" role="status" style={{ marginTop: 16 }}>
          <strong>{formatNumber(health.jobs_failed)} job(s) are in a failed state.</strong>{" "}
          <Link href="/jobs?state=FAILED">Open the queue</Link> to read the error and retry.
        </div>
      ) : null}

      <h3>Headline</h3>
      <div className="grid grid-hero">
        <Stat
          label="Active students"
          value={counts.students_active_in_window}
          hint={`of ${formatNumber(counts.students_total)} standalone identities`}
          tone="accent"
          share={counts.students_total > 0 ? counts.students_active_in_window / counts.students_total : 0}
        />
        <Stat
          label="Requests"
          value={counts.requests}
          hint={`${formatNumber(counts.questions_answered)} student turns`}
        />
        <MoneyStat
          label="Spend"
          micros={counts.cost_micros}
          hint={`${formatNumber(counts.model_calls)} model calls, at call-time rates`}
        />
        <Stat
          label="Latency p95"
          value={formatMs(counts.latency_p95_ms)}
          hint={`p50 ${formatMs(counts.latency_p50_ms)} · inbound event to terminal state`}
        />
      </div>

      <h3>Reliability</h3>
      <div className="grid">
        <Stat
          label="Failed requests"
          value={counts.requests_failed}
          hint={share(counts.requests_failed, counts.requests, "of requests", "no requests yet")}
          tone={failureTone}
        />
        <Stat
          label="Quota blocks"
          value={counts.quota_blocked}
          hint="refused before spending anything"
        />
        <Stat
          label="Failed jobs"
          value={counts.jobs_failed}
          hint={`${formatNumber(health.jobs_pending)} pending now`}
          tone={jobsTone}
        />
        <Stat label="Open conversations" value={health.conversations_open} />
        <Stat
          label="Awaiting grade"
          value={health.attempts_awaiting_grade}
          hint="submitted attempts"
          tone={gradeTone}
        />
      </div>

      <h3>Cost and model use</h3>
      <div className="grid">
        <Stat label="Model calls" value={counts.model_calls} />
        <Stat
          label="Verifier calls"
          value={counts.verifier_calls}
          hint={share(counts.verifier_calls, counts.model_calls, "of all calls", "no calls yet")}
          share={counts.model_calls > 0 ? counts.verifier_calls / counts.model_calls : undefined}
        />
        <Stat
          label="Prompt cache hits"
          value={counts.input_tokens > 0 ? `${Math.round(cacheRate * 100)}%` : "—"}
          hint={`${formatNumber(counts.cached_input_tokens)} of ${formatNumber(
            counts.input_tokens,
          )} input tokens`}
          tone={counts.input_tokens > 0 && cacheRate >= 0.2 ? "ok" : undefined}
          share={counts.input_tokens > 0 ? cacheRate : undefined}
        />
        <Stat
          label="Extraction cache"
          value={counts.extraction_cache_entries}
          hint="reusable entries written in window"
        />
      </div>

      <h3>Media and knowledge</h3>
      <div className="grid">
        <Stat label="Media objects" value={counts.media_objects} />
        <Stat
          label="Local extractions"
          value={counts.local_extractions}
          hint={share(counts.local_extractions, totalExtractions, "done without a model", "no extractions yet")}
          tone={totalExtractions > 0 && counts.local_extractions >= counts.vision_escalations ? "ok" : undefined}
          share={totalExtractions > 0 ? counts.local_extractions / totalExtractions : undefined}
        />
        <Stat
          label="Vision escalations"
          value={counts.vision_escalations}
          hint="paid image reads"
          tone={
            totalExtractions > 0 && counts.vision_escalations > counts.local_extractions
              ? "warn"
              : undefined
          }
        />
        <Stat label="Mock tests" value={counts.mock_tests} />
        <Stat label="RAG sources" value={counts.rag_sources} />
        <Stat
          label="RAG chunks"
          value={counts.rag_chunks}
          hint="embedded and searchable"
        />
      </div>

      <h3>Spend by model alias</h3>
      {data.cost_by_alias.length === 0 ? (
        <Empty
          title="No model calls in this window"
          hint="Either nothing was asked, or every answer came from a deterministic path."
        />
      ) : (
        <DataTable
          caption="Spend by model alias"
          columns={[
            { key: "alias", label: "Alias" },
            { key: "provider", label: "Provider" },
            { key: "calls", label: "Calls", numeric: true },
            { key: "in", label: "Input tokens", numeric: true },
            { key: "out", label: "Output tokens", numeric: true },
            { key: "cached", label: "Cached", numeric: true },
            { key: "cost", label: "Cost", numeric: true },
            { key: "bar", label: "Share of spend" },
          ]}
        >
          {data.cost_by_alias.map((row) => (
            <tr key={`${row.model_alias}-${row.provider}`}>
              <td>
                <strong>{row.model_alias}</strong>
              </td>
              <td>{row.provider}</td>
              <td className="numeric">{formatNumber(row.calls)}</td>
              <td className="numeric">{formatNumber(row.input_tokens)}</td>
              <td className="numeric">{formatNumber(row.output_tokens)}</td>
              <td className="numeric">{formatNumber(row.cached_tokens)}</td>
              <td className="numeric">{formatMicros(row.cost_micros)}</td>
              <td style={{ width: 180 }}>
                <div className="bar-row">
                  <ShareBar share={row.cost_micros / maxAliasCost} />
                  <span className="hint" style={{ minWidth: 38, textAlign: "right" }}>
                    {counts.cost_micros > 0
                      ? `${Math.round((100 * row.cost_micros) / counts.cost_micros)}%`
                      : "—"}
                  </span>
                </div>
              </td>
            </tr>
          ))}
        </DataTable>
      )}

      <h3>Failures by code</h3>
      {data.failures_by_code.length === 0 ? (
        <Empty
          title="No failed requests in this window"
          hint="Every request that started reached a completed terminal state."
        />
      ) : (
        <DataTable
          caption="Failures by error code"
          columns={[
            { key: "code", label: "Error code" },
            { key: "count", label: "Count", numeric: true },
            { key: "bar", label: "Relative frequency" },
          ]}
        >
          {data.failures_by_code.map((row) => (
            <tr key={row.error_code}>
              <td className="mono">{row.error_code}</td>
              <td className="numeric">{formatNumber(row.count)}</td>
              <td style={{ width: 240 }}>
                <ShareBar share={row.count / maxFailures} tone="bad" />
              </td>
            </tr>
          ))}
        </DataTable>
      )}
    </>
  );
}
