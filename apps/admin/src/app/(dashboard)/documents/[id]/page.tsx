import Link from "next/link";
import { notFound } from "next/navigation";

import { ActionForm } from "@/components/action-form";
import { Badge, BoolBadge, DataTable, Empty, HighRiskFields, PageHeader } from "@/components/ui";
import { deleteDocumentAction, reprocessDocumentAction } from "@/lib/actions";
import { ApiError, apiFetch } from "@/lib/api";
import { currentActor } from "@/lib/auth-actions";
import { formatDateTime, formatNumber } from "@/lib/format";
import { can } from "@/lib/session";
import type { DocumentDetail } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function DocumentPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  let data: DocumentDetail;
  try {
    data = await apiFetch<DocumentDetail>(`/v1/admin/documents/${id}`);
  } catch (error) {
    if (error instanceof ApiError && error.isNotFound) notFound();
    throw error;
  }

  const actor = await currentActor();
  const doc = data.document;

  return (
    <>
      <PageHeader
        title={doc.title}
        subtitle={`${doc.kind} · ${doc.visibility} · ingested ${formatDateTime(doc.created_at)}`}
        actions={<Link href="/documents">← All documents</Link>}
      />

      <dl className="kv">
        <dt>Status</dt>
        <dd>
          <Badge value={doc.status} />
        </dd>
        <dt>Owner</dt>
        <dd>
          {doc.owner_subject_id ? (
            <Link href={`/students/${doc.owner_subject_id}`}>
              {doc.owner_identity ?? doc.owner_subject_id}
            </Link>
          ) : (
            "not student-owned"
          )}
        </dd>
        <dt>Chunks</dt>
        <dd>{formatNumber(doc.chunk_count)}</dd>
        <dt>Content hash</dt>
        <dd className="mono">{doc.content_sha256}</dd>
        <dt>Parser / chunker</dt>
        <dd className="mono">
          {doc.parser_version} / {doc.chunker_version}
        </dd>
        <dt>Embedding model</dt>
        <dd className="mono">{doc.embedding_model}</dd>
        <dt>Deleted</dt>
        <dd>{doc.deleted_at ? formatDateTime(doc.deleted_at) : "no"}</dd>
      </dl>

      <h3>Chunks</h3>
      {data.chunks.length === 0 ? (
        <Empty
          title="No chunks"
          hint="Either extraction has not run, or the document produced no usable text."
        />
      ) : (
        <DataTable
          caption="Document chunks"
          columns={[
            { key: "ordinal", label: "#", numeric: true },
            { key: "page", label: "Page", numeric: true },
            { key: "section", label: "Section" },
            { key: "tokens", label: "Tokens", numeric: true },
            { key: "embedded", label: "Embedded" },
            { key: "text", label: "Text", wrap: true },
          ]}
        >
          {data.chunks.map((chunk) => (
            <tr key={chunk.id}>
              <td className="numeric">{chunk.ordinal}</td>
              <td className="numeric">{chunk.page_number ?? "—"}</td>
              <td>{chunk.section ?? "—"}</td>
              <td className="numeric">{chunk.token_estimate}</td>
              <td>
                <BoolBadge value={chunk.has_embedding} on="yes" off="no" />
              </td>
              <td className="wrap">{chunk.text_preview}</td>
            </tr>
          ))}
        </DataTable>
      )}

      {can(actor, "document:write") ? (
        <>
          <h3>High-risk actions</h3>

          <details className="risk">
            <summary>Reprocess this document</summary>
            <p style={{ marginTop: 0 }}>
              Re-embedding is a real bill: {formatNumber(doc.chunk_count)} chunk(s) would
              be embedded again.
            </p>
            <ActionForm action={reprocessDocumentAction} submitLabel="Queue re-ingestion">
              <input type="hidden" name="document_id" value={doc.id} />
              <HighRiskFields actionLabel="re-embeds the whole document" />
            </ActionForm>
          </details>

          <details className="risk">
            <summary>Remove from retrieval</summary>
            <p style={{ marginTop: 0 }}>
              A soft delete. Retrieval already excludes deleted sources, and the row
              stays readable for audit.
            </p>
            <ActionForm action={deleteDocumentAction} submitLabel="Remove document" danger>
              <input type="hidden" name="document_id" value={doc.id} />
              <HighRiskFields actionLabel="removes this document from every answer" />
            </ActionForm>
          </details>
        </>
      ) : null}
    </>
  );
}
