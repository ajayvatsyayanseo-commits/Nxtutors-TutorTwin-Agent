import Link from "next/link";
import { notFound } from "next/navigation";

import { Badge, DataTable, Empty, MoneyStat, PageHeader, Stat } from "@/components/ui";
import { ApiError, apiFetch } from "@/lib/api";
import { formatDateTime, formatMicros, formatNumber } from "@/lib/format";
import type { ConversationTimeline } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function ConversationPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  let data: ConversationTimeline;
  try {
    data = await apiFetch<ConversationTimeline>(`/v1/admin/conversations/${id}`);
  } catch (error) {
    if (error instanceof ApiError && error.isNotFound) notFound();
    throw error;
  }

  const { conversation } = data;

  // Latency of the slowest completed turn: an operator opening a conversation
  // after a complaint wants the worst one, not the average of it with fast ones.
  const latencies = data.requests
    .map((request) => request.latency_ms)
    .filter((value): value is number => value !== null);
  const slowest = latencies.length > 0 ? Math.max(...latencies) : null;
  const verifications = data.model_calls.filter((call) => call.is_verification).length;

  return (
    <>
      <PageHeader
        title={`Conversation with ${conversation.student_identity}`}
        subtitle={`${conversation.source} · ${conversation.message_count} message(s) · last active ${formatDateTime(
          conversation.last_activity_at,
        )}`}
        actions={
          <>
            <Link href={`/students/${conversation.subject_id}`}>Student →</Link>
            <Link href="/conversations">← All conversations</Link>
          </>
        }
      />

      <div className="grid">
        <Stat label="Requests" value={data.requests.length} />
        <Stat
          label="Slowest turn"
          value={
            slowest === null
              ? "—"
              : slowest < 1000
                ? `${formatNumber(slowest)} ms`
                : `${(slowest / 1000).toFixed(1)} s`
          }
          hint="event received to terminal state"
        />
        <Stat label="Model calls" value={data.model_calls.length} />
        <Stat
          label="Verifications"
          value={verifications}
          hint={verifications > 0 ? "second-model answer checks" : "no answer was re-checked"}
        />
        <MoneyStat label="Cost" micros={data.cost_micros} />
        <Stat label="Outbound actions" value={data.outbound.length} />
      </div>

      <h3>Timeline</h3>
      {data.messages.length === 0 ? (
        <Empty title="No messages recorded" />
      ) : (
        <div className="timeline">
          {data.messages.map((message) => (
            <article
              key={message.id}
              className={`turn ${message.role === "ASSISTANT" ? "assistant" : "student"}`}
            >
              <header>
                <Badge value={message.role} />
                <span>{message.input_type}</span>
                {message.capability ? <span>· {message.capability}</span> : null}
                <span>· {formatDateTime(message.created_at)}</span>
              </header>
              {/* Normalised text is shown; media stays a reference. An operator
                  diagnosing a failure needs the words, not the photograph. */}
              <p>{message.text ?? <em>(no text — see media reference below)</em>}</p>
              {message.media ? (
                <p className="mono" style={{ color: "var(--text-muted)", marginTop: 6 }}>
                  media: {JSON.stringify(message.media)}
                </p>
              ) : null}
              {Object.keys(message.safety_flags ?? {}).length > 0 ? (
                <p className="mono" style={{ color: "var(--warning)", marginTop: 6 }}>
                  safety: {JSON.stringify(message.safety_flags)}
                </p>
              ) : null}
            </article>
          ))}
        </div>
      )}

      <h3>Requests</h3>
      {data.requests.length === 0 ? (
        <Empty title="No request events" />
      ) : (
        <DataTable
          caption="Request events"
          columns={[
            { key: "request", label: "Request id" },
            { key: "correlation", label: "Correlation id" },
            { key: "type", label: "Type" },
            { key: "status", label: "Status" },
            { key: "error", label: "Error" },
            { key: "latency", label: "Latency", numeric: true },
            { key: "at", label: "Received" },
          ]}
        >
          {data.requests.map((row) => (
            <tr key={row.id}>
              <td className="mono">{row.request_id}</td>
              <td className="mono">{row.correlation_id}</td>
              <td>{row.message_type}</td>
              <td>
                <Badge value={row.status} />
              </td>
              <td>{row.error_code ?? "—"}</td>
              <td className="numeric">
                {row.latency_ms === null ? "in flight" : `${formatNumber(row.latency_ms)} ms`}
              </td>
              <td>{formatDateTime(row.created_at)}</td>
            </tr>
          ))}
        </DataTable>
      )}

      <h3>Model calls</h3>
      {data.model_calls.length === 0 ? (
        <Empty
          title="No paid model calls"
          hint="Either the answer came from a deterministic path, or the budget gate refused."
        />
      ) : (
        <DataTable
          caption="Model calls"
          columns={[
            { key: "alias", label: "Alias" },
            { key: "provider", label: "Provider" },
            { key: "model", label: "Model id" },
            { key: "capability", label: "For" },
            { key: "in", label: "Input", numeric: true },
            { key: "out", label: "Output", numeric: true },
            { key: "cached", label: "Cached", numeric: true },
            { key: "cost", label: "Cost", numeric: true },
            { key: "rate", label: "Rate version" },
          ]}
        >
          {data.model_calls.map((row) => (
            <tr key={row.id}>
              <td>{row.model_alias}</td>
              <td>{row.provider}</td>
              <td className="mono">{row.model_id ?? "—"}</td>
              <td>
                {row.is_verification ? (
                  <span className="badge badge-accent">verification</span>
                ) : (
                  (row.capability ?? "—")
                )}
              </td>
              <td className="numeric">{formatNumber(row.input_tokens)}</td>
              <td className="numeric">{formatNumber(row.output_tokens)}</td>
              <td className="numeric">{formatNumber(row.cached_tokens)}</td>
              <td className="numeric">{formatMicros(row.cost_micros)}</td>
              <td>{row.rate_version ?? "—"}</td>
            </tr>
          ))}
        </DataTable>
      )}

      <h3>RAG retrieval</h3>
      {data.retrievals.length === 0 ? (
        <Empty
          title="No retrieval was attempted"
          hint="Retrieval runs only when the question looks like it needs a document."
        />
      ) : (
        <DataTable
          caption="Retrieval events"
          columns={[
            { key: "performed", label: "Performed" },
            { key: "reason", label: "Skip reason" },
            { key: "returned", label: "Chunks used", numeric: true },
            { key: "scanned", label: "Candidates", numeric: true },
            { key: "embed", label: "Embedding calls", numeric: true },
            { key: "ms", label: "Query", numeric: true },
            { key: "sources", label: "Chunk ids", wrap: true },
          ]}
        >
          {data.retrievals.map((row) => (
            <tr key={row.id}>
              <td>
                <Badge value={row.performed ? "PERFORMED" : "SKIPPED"} />
              </td>
              <td>{row.skip_reason ?? "—"}</td>
              <td className="numeric">
                {formatNumber(row.returned)} of {formatNumber(row.top_k)}
              </td>
              <td className="numeric">{formatNumber(row.candidates_scanned)}</td>
              <td className="numeric">{formatNumber(row.embedding_calls)}</td>
              <td className="numeric">{formatNumber(row.query_ms)} ms</td>
              {/* Ids, not text and never vectors: this answers "which chunk",
                  and the document page answers "what did it say". */}
              <td className="wrap mono">
                {row.chunk_ids.length === 0 ? "—" : row.chunk_ids.join(", ")}
              </td>
            </tr>
          ))}
        </DataTable>
      )}

      <h3>Outbound actions</h3>
      {data.outbound.length === 0 ? (
        <Empty title="Nothing was sent" />
      ) : (
        <DataTable
          caption="Outbound actions"
          columns={[
            { key: "type", label: "Type" },
            { key: "delivery", label: "Delivery" },
            { key: "at", label: "Created" },
          ]}
        >
          {data.outbound.map((row) => (
            <tr key={row.id}>
              <td>{row.action_type}</td>
              <td>
                <Badge value={row.delivery_status} />
              </td>
              <td>{formatDateTime(row.created_at)}</td>
            </tr>
          ))}
        </DataTable>
      )}
    </>
  );
}
