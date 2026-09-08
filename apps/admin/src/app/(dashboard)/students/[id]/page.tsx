import Link from "next/link";
import { notFound } from "next/navigation";

import { StudentActions } from "./actions-panel";
import { ActionForm } from "@/components/action-form";
import { Badge, DataTable, Empty, MoneyStat, PageHeader, Stat } from "@/components/ui";
import { assignStudentAction } from "@/lib/actions";
import { ApiError, apiFetch } from "@/lib/api";
import { currentActor } from "@/lib/auth-actions";
import { formatAccuracy, formatDateTime, formatNumber, truncate } from "@/lib/format";
import { can } from "@/lib/session";
import type { StudentDetail, TutorSummary } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function StudentPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  let data: StudentDetail;
  try {
    data = await apiFetch<StudentDetail>(`/v1/admin/students/${id}`);
  } catch (error) {
    if (error instanceof ApiError && error.isNotFound) notFound();
    throw error;
  }

  const actor = await currentActor();
  const student = data.student;
  const mayAssign = can(actor, "tutor:write");

  // Assigning from here is the flow an operator actually has: they arrive at a
  // student from a support ticket, not at a tutor. The endpoint is the same one
  // the tutor page posts to, so there is one assignment rule and one audit event.
  const tutors = mayAssign
    ? await apiFetch<TutorSummary[]>("/v1/admin/tutors")
    : null;

  return (
    <>
      <PageHeader
        title={student.external_identity_value}
        subtitle={`${student.external_identity_type} · created ${formatDateTime(
          student.created_at,
        )}`}
        actions={<Link href="/students">← All students</Link>}
      />

      <div className="grid">
        <Stat label="Requests" value={data.usage.requests} />
        <Stat label="Model calls" value={data.usage.model_calls} />
        <MoneyStat label="Lifetime spend" micros={data.usage.cost_micros} />
        <Stat label="Documents" value={data.documents.length} />
        <Stat label="Assessments" value={data.assessments.length} />
        <Stat label="Memories" value={data.memories.length} />
      </div>

      <dl className="kv">
        <dt>Student id</dt>
        <dd className="mono">{student.id}</dd>
        <dt>Status</dt>
        <dd>
          <Badge value={student.status} />
        </dd>
        <dt>Assigned tutor</dt>
        <dd>
          {student.tutor_id ? (
            <Link href={`/tutors/${student.tutor_id}`}>{student.tutor_name}</Link>
          ) : (
            <span className="badge badge-warn">none</span>
          )}
        </dd>
      </dl>

      {mayAssign ? (
        <>
          <h3>Tutor assignment</h3>
          <fieldset>
            <legend>{student.tutor_name ? "Reassign tutor" : "Assign a tutor"}</legend>
            {tutors && tutors.length === 0 ? (
              <Empty
                title="No tutors exist yet"
                hint="Create one on the Tutors page, then come back."
              />
            ) : (
              <>
                <p className="subtitle" style={{ marginBottom: 12 }}>
                  A student has one active tutor. Assigning replaces the current one and
                  records the reason in the audit log.
                </p>
                <ActionForm action={assignStudentAction} submitLabel="Assign tutor">
                  <input type="hidden" name="subject_id" value={student.id} />
                  <div className="form-grid">
                    <div className="field">
                      <label htmlFor="tutor_id">Tutor</label>
                      <select
                        id="tutor_id"
                        name="tutor_id"
                        required
                        defaultValue={student.tutor_id ?? ""}
                      >
                        <option value="">Select a tutor…</option>
                        {(tutors ?? []).map((tutor) => (
                          <option key={tutor.id} value={tutor.id}>
                            {tutor.display_name}
                            {tutor.active_persona_version
                              ? ` (persona v${tutor.active_persona_version})`
                              : " (no active persona)"}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div className="field">
                      <label htmlFor="assign_reason">Reason</label>
                      <input id="assign_reason" name="reason" required maxLength={500} />
                    </div>
                  </div>
                </ActionForm>
              </>
            )}
          </fieldset>
        </>
      ) : null}

      <h3>Entitlements</h3>
      {data.entitlements.length === 0 ? (
        <Empty title="No entitlement recorded" hint="The student has never been resolved." />
      ) : (
        <DataTable
          caption="Entitlement history"
          columns={[
            { key: "plan", label: "Plan" },
            { key: "status", label: "Status" },
            { key: "source", label: "Source" },
            { key: "ends", label: "Ends" },
            { key: "fetched", label: "Fetched" },
          ]}
        >
          {data.entitlements.map((row) => (
            <tr key={row.id}>
              <td>{row.plan_code}</td>
              <td>
                <Badge value={row.status} />
              </td>
              <td>{row.source}</td>
              <td>{formatDateTime(row.ends_at)}</td>
              <td>{formatDateTime(row.fetched_at)}</td>
            </tr>
          ))}
        </DataTable>
      )}

      <h3>Conversations</h3>
      {data.conversations.length === 0 ? (
        <Empty title="No conversations" />
      ) : (
        <DataTable
          caption="Conversations"
          columns={[
            { key: "id", label: "Conversation" },
            { key: "status", label: "Status" },
            { key: "source", label: "Source" },
            { key: "activity", label: "Last activity" },
          ]}
        >
          {data.conversations.map((row) => (
            <tr key={row.id}>
              <td>
                <Link href={`/conversations/${row.id}`} className="mono">
                  {row.id}
                </Link>
              </td>
              <td>
                <Badge value={row.status} />
              </td>
              <td>{row.source}</td>
              <td>{formatDateTime(row.last_activity_at)}</td>
            </tr>
          ))}
        </DataTable>
      )}

      <h3>Documents</h3>
      {data.documents.length === 0 ? (
        <Empty title="No documents uploaded" />
      ) : (
        <DataTable
          caption="Documents"
          columns={[
            { key: "title", label: "Title", wrap: true },
            { key: "kind", label: "Kind" },
            { key: "status", label: "Status" },
            { key: "chunks", label: "Chunks", numeric: true },
          ]}
        >
          {data.documents.map((row) => (
            <tr key={row.id}>
              <td className="wrap">
                <Link href={`/documents/${row.id}`}>{row.title}</Link>
                {row.deleted_at ? <span className="badge badge-bad"> deleted</span> : null}
              </td>
              <td>{row.kind}</td>
              <td>
                <Badge value={row.status} />
              </td>
              <td className="numeric">{formatNumber(row.chunk_count)}</td>
            </tr>
          ))}
        </DataTable>
      )}

      <h3>Assessments</h3>
      {data.assessments.length === 0 ? (
        <Empty title="No quizzes or mock tests" />
      ) : (
        <DataTable
          caption="Assessments"
          columns={[
            { key: "title", label: "Title", wrap: true },
            { key: "kind", label: "Kind" },
            { key: "duration", label: "Minutes", numeric: true },
            { key: "marks", label: "Marks", numeric: true },
            { key: "truncated", label: "Plan limit", wrap: true },
          ]}
        >
          {data.assessments.map((row) => (
            <tr key={row.id}>
              <td className="wrap">
                <Link href={`/learning/${row.id}`}>{row.title}</Link>
              </td>
              <td>{row.kind}</td>
              <td className="numeric">{row.duration_minutes}</td>
              <td className="numeric">{row.total_marks}</td>
              <td className="wrap">{row.truncated_reason ?? "—"}</td>
            </tr>
          ))}
        </DataTable>
      )}

      <h3>Progress</h3>
      {data.progress.length === 0 ? (
        <Empty
          title="No practice recorded"
          hint="Progress appears once the student attempts questions."
        />
      ) : (
        <DataTable
          caption="Topic progress"
          columns={[
            { key: "topic", label: "Topic" },
            { key: "attempts", label: "Attempts", numeric: true },
            { key: "correct", label: "Correct", numeric: true },
            { key: "hints", label: "Hints", numeric: true },
            { key: "accuracy", label: "Accuracy", numeric: true },
            { key: "seen", label: "Last seen" },
          ]}
        >
          {data.progress.map((row) => (
            <tr key={row.topic}>
              <td>{row.topic}</td>
              <td className="numeric">{row.attempts}</td>
              <td className="numeric">{row.correct}</td>
              <td className="numeric">{row.hints_used}</td>
              {/* Below the evidence threshold this reads "not enough data", never
                  0% — which would say "always wrong" instead. */}
              <td className="numeric">{formatAccuracy(row.attempts, row.correct)}</td>
              <td>{formatDateTime(row.last_seen_at)}</td>
            </tr>
          ))}
        </DataTable>
      )}

      <h3>Memories</h3>
      {data.memories.length === 0 ? (
        <Empty title="No durable memories" hint="Memory is facts worth keeping, not a transcript." />
      ) : (
        <DataTable
          caption="Student memories"
          columns={[
            { key: "kind", label: "Kind" },
            { key: "statement", label: "Statement", wrap: true },
            { key: "confidence", label: "Confidence" },
            { key: "observed", label: "Seen", numeric: true },
            { key: "derived", label: "Derived by" },
          ]}
        >
          {data.memories.map((row) => (
            <tr key={row.id}>
              <td>{row.kind}</td>
              <td className="wrap">{truncate(row.statement, 160)}</td>
              <td>
                <Badge value={row.confidence} />
              </td>
              <td className="numeric">{row.observed_count}</td>
              <td>{row.derived_by}</td>
            </tr>
          ))}
        </DataTable>
      )}

      {can(actor, "student:write") ? (
        <>
          <h3>High-risk actions</h3>
          <StudentActions
            studentId={student.id}
            identity={student.external_identity_value}
            currentPlan={data.entitlements[0]?.plan_code ?? ""}
          />
        </>
      ) : null}
    </>
  );
}
