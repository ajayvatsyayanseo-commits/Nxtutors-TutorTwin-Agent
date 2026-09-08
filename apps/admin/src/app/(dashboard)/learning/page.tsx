import Link from "next/link";

import { Badge, DataTable, Empty, PageHeader, Pagination } from "@/components/ui";
import { apiFetch } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import type { AssessmentSummary, Page } from "@/lib/types";

export const metadata = { title: "Learning · TutorTwin" };
export const dynamic = "force-dynamic";

export default async function LearningPage({
  searchParams,
}: {
  searchParams: Promise<{ kind?: string; student_id?: string; page?: string }>;
}) {
  const params = await searchParams;
  const page = Math.max(1, Number(params.page ?? 1) || 1);

  const data = await apiFetch<Page<AssessmentSummary>>("/v1/admin/learning/assessments", {
    query: {
      kind: params.kind,
      student_id: params.student_id,
      page,
      page_size: 25,
    },
  });

  const filtered = Boolean(params.kind || params.student_id);

  return (
    <>
      <PageHeader
        title="Learning"
        subtitle="Quizzes, mock tests and their attempts. Open one to see the key and the grading evidence."
      />

      <form className="filters" method="get">
        <div className="field">
          <label htmlFor="kind">Kind</label>
          <select id="kind" name="kind" defaultValue={params.kind ?? ""}>
            <option value="">Any</option>
            <option value="QUIZ">Quiz</option>
            <option value="MOCK_TEST">Mock test</option>
            <option value="PRACTICE_SET">Practice set</option>
          </select>
        </div>
        <div className="field">
          <label htmlFor="student_id">Student id</label>
          <input id="student_id" name="student_id" defaultValue={params.student_id ?? ""} />
        </div>
        <button type="submit" className="primary">
          Apply
        </button>
        {filtered ? <Link href="/learning">Clear</Link> : null}
      </form>

      {data.items.length === 0 ? (
        <Empty title={filtered ? "Nothing matches those filters" : "No assessments yet"} />
      ) : (
        <>
          <DataTable
            caption="Assessments"
            columns={[
              { key: "title", label: "Title", wrap: true },
              { key: "kind", label: "Kind" },
              { key: "student", label: "Student" },
              { key: "topic", label: "Topic" },
              { key: "minutes", label: "Minutes", numeric: true },
              { key: "marks", label: "Marks", numeric: true },
              { key: "attempts", label: "Attempts", numeric: true },
              { key: "limit", label: "Plan limit", wrap: true },
              { key: "created", label: "Created" },
            ]}
          >
            {data.items.map((row) => (
              <tr key={row.id}>
                <td className="wrap">
                  <Link href={`/learning/${row.id}`}>{row.title}</Link>
                </td>
                <td>
                  <Badge value={row.kind} />
                </td>
                <td>
                  <Link href={`/students/${row.subject_id}`}>{row.student_identity}</Link>
                </td>
                <td>{row.topic ?? "—"}</td>
                <td className="numeric">{row.duration_minutes}</td>
                <td className="numeric">{row.total_marks}</td>
                <td className="numeric">{row.attempts}</td>
                <td className="wrap">{row.truncated_reason ?? "—"}</td>
                <td>{formatDateTime(row.created_at)}</td>
              </tr>
            ))}
          </DataTable>
          <Pagination
            page={data.page}
            pageSize={data.page_size}
            total={data.total}
            basePath="/learning"
            query={{ kind: params.kind, student_id: params.student_id }}
          />
        </>
      )}
    </>
  );
}
