import Link from "next/link";
import { notFound } from "next/navigation";

import { Badge, DataTable, Empty, PageHeader, Stat } from "@/components/ui";
import { ApiError, apiFetch } from "@/lib/api";
import { formatDateTime } from "@/lib/format";

export const dynamic = "force-dynamic";

interface AssessmentDetail {
  assessment: {
    id: string;
    kind: string;
    title: string;
    topic: string | null;
    duration_minutes: number;
    total_marks: number;
    blueprint: Record<string, unknown> | null;
    truncated_reason: string | null;
    created_at: string;
  };
  questions: {
    number: number;
    question_type: string;
    prompt: string;
    marks: number;
    options: string[] | null;
    answer_key: Record<string, unknown> | null;
  }[];
  attempts: {
    id: string;
    state: string;
    awarded_marks: number | null;
    total_marks: number;
    manual_review: boolean;
    model_calls: number;
    started_at: string;
    submitted_at: string | null;
    responses: {
      number: number;
      awarded_marks: number;
      max_marks: number;
      correct: boolean | null;
      feedback: string | null;
      evidence: string | null;
      graded_by: string;
      needs_review: boolean;
    }[];
  }[];
}

/**
 * A mock test as an operator sees it — **including the answer key**.
 *
 * That is deliberate and is the opposite of the student path. Verifying that
 * grading was correct is impossible without the key, so the difference between
 * the two audiences is authorisation, not an omitted column: the student
 * projection has no field able to hold a key, while this route sits behind
 * `learning:read`.
 */
export default async function AssessmentPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  let data: AssessmentDetail;
  try {
    data = await apiFetch<AssessmentDetail>(`/v1/admin/learning/assessments/${id}`);
  } catch (error) {
    if (error instanceof ApiError && error.isNotFound) notFound();
    throw error;
  }

  const { assessment } = data;
  const modelCalls = data.attempts.reduce((sum, attempt) => sum + attempt.model_calls, 0);

  return (
    <>
      <PageHeader
        title={assessment.title}
        subtitle={`${assessment.kind} · ${assessment.duration_minutes} minutes · ${assessment.total_marks} marks`}
        actions={<Link href="/learning">← All assessments</Link>}
      />

      {assessment.truncated_reason ? (
        <div className="alert alert-warning" role="status">
          <strong>Reduced to fit the plan:</strong> {assessment.truncated_reason}
        </div>
      ) : null}

      <div className="grid">
        <Stat label="Questions" value={data.questions.length} />
        <Stat label="Attempts" value={data.attempts.length} />
        <Stat
          label="Model calls"
          value={modelCalls}
          hint="objective grading costs none"
        />
        <Stat label="Created" value={formatDateTime(assessment.created_at)} />
      </div>

      {assessment.blueprint ? (
        <dl className="kv">
          <dt>Blueprint</dt>
          <dd className="mono">{JSON.stringify(assessment.blueprint)}</dd>
        </dl>
      ) : null}

      <h3>Questions and answer key</h3>
      <DataTable
        caption="Questions"
        columns={[
          { key: "n", label: "#", numeric: true },
          { key: "type", label: "Type" },
          { key: "prompt", label: "Prompt", wrap: true },
          { key: "marks", label: "Marks", numeric: true },
          { key: "options", label: "Options", wrap: true },
          { key: "key", label: "Answer key", wrap: true },
        ]}
      >
        {data.questions.map((question) => (
          <tr key={question.number}>
            <td className="numeric">{question.number}</td>
            <td>{question.question_type}</td>
            <td className="wrap">{question.prompt}</td>
            <td className="numeric">{question.marks}</td>
            <td className="wrap">{(question.options ?? []).join(" · ") || "—"}</td>
            <td className="wrap mono">{JSON.stringify(question.answer_key ?? {})}</td>
          </tr>
        ))}
      </DataTable>

      <h3>Attempts</h3>
      {data.attempts.length === 0 ? (
        <Empty title="Not attempted yet" />
      ) : (
        data.attempts.map((attempt) => (
          <section key={attempt.id}>
            <h3 style={{ marginTop: 20 }}>
              Attempt {attempt.id.slice(0, 8)} <Badge value={attempt.state} />
              {attempt.manual_review ? (
                <span className="badge badge-warn" style={{ marginLeft: 6 }}>
                  manual review
                </span>
              ) : null}
            </h3>
            <dl className="kv">
              <dt>Score</dt>
              <dd>
                {attempt.awarded_marks ?? "—"} / {attempt.total_marks}
              </dd>
              <dt>Model calls</dt>
              <dd>{attempt.model_calls}</dd>
              <dt>Submitted</dt>
              <dd>{formatDateTime(attempt.submitted_at)}</dd>
            </dl>
            {attempt.responses.length === 0 ? (
              <Empty title="No graded responses" />
            ) : (
              <DataTable
                caption={`Responses for attempt ${attempt.id}`}
                columns={[
                  { key: "n", label: "#", numeric: true },
                  { key: "marks", label: "Marks", numeric: true },
                  { key: "correct", label: "Correct" },
                  { key: "graded", label: "Graded by" },
                  { key: "evidence", label: "Evidence", wrap: true },
                  { key: "feedback", label: "Feedback", wrap: true },
                ]}
              >
                {attempt.responses.map((response) => (
                  <tr key={response.number}>
                    <td className="numeric">{response.number}</td>
                    <td className="numeric">
                      {response.awarded_marks} / {response.max_marks}
                    </td>
                    <td>
                      {/* `null` means partial credit on a subjective question —
                          not "wrong". Rendering it as false would misreport it. */}
                      {response.correct === null ? (
                        <span className="badge">partial</span>
                      ) : (
                        <Badge value={response.correct ? "COMPLETED" : "FAILED"} />
                      )}
                    </td>
                    <td>{response.graded_by}</td>
                    <td className="wrap">{response.evidence ?? "—"}</td>
                    <td className="wrap">{response.feedback ?? "—"}</td>
                  </tr>
                ))}
              </DataTable>
            )}
          </section>
        ))
      )}
    </>
  );
}
