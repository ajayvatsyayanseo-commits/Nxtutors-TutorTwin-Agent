import Link from "next/link";
import { notFound } from "next/navigation";

import { ActionForm } from "@/components/action-form";
import { Badge, DataTable, Empty, HighRiskFields, PageHeader } from "@/components/ui";
import {
  activatePersonaAction,
  assignStudentAction,
  createPersonaDraftAction,
} from "@/lib/actions";
import { ApiError, apiFetch } from "@/lib/api";
import { currentActor } from "@/lib/auth-actions";
import { formatDateTime } from "@/lib/format";
import { can } from "@/lib/session";
import type { Page, StudentSummary, TutorDetail } from "@/lib/types";

export const dynamic = "force-dynamic";

const PEDAGOGY_MODES = [
  "GUIDED",
  "HINT_FIRST",
  "STEP_BY_STEP",
  "ANSWER_AND_EXPLAIN",
  "SOCRATIC",
  "EXAM_REVISION",
];

const NOTIFICATION_PREFERENCES = ["none", "daily", "weekly"];

/** How many identities the assignment picker offers before it needs a search. */
const PICKER_SIZE = 200;

function personaField(persona: Record<string, unknown>, key: string): string {
  const value = persona[key];
  if (Array.isArray(value)) return value.join(", ") || "—";
  return value === undefined || value === null || value === "" ? "—" : String(value);
}

/** The same value, but blank rather than an em dash — for prefilling an input. */
function personaInput(persona: Record<string, unknown> | undefined, key: string): string {
  if (!persona) return "";
  const value = persona[key];
  if (Array.isArray(value)) return value.join(", ");
  return value === undefined || value === null ? "" : String(value);
}

export default async function TutorPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;

  let data: TutorDetail;
  try {
    data = await apiFetch<TutorDetail>(`/v1/admin/tutors/${id}`);
  } catch (error) {
    if (error instanceof ApiError && error.isNotFound) notFound();
    throw error;
  }

  const actor = await currentActor();
  const active = data.personas.find((persona) => persona.is_active);
  const latest = data.personas[0];
  const mayWrite = can(actor, "tutor:write");

  // The picker only exists for an operator who can actually assign, so the
  // student list is not fetched for anyone else — a page that reads data it will
  // never show is a permission leak waiting to be introduced.
  const students = mayWrite
    ? await apiFetch<Page<StudentSummary>>("/v1/admin/students", {
        query: { page_size: PICKER_SIZE },
      })
    : null;

  // A new draft starts from the newest version rather than from blank. Retyping
  // nine fields to change a tone is how a persona loses its other eight.
  const seed = latest?.persona;

  return (
    <>
      <PageHeader
        title={data.tutor.display_name}
        subtitle={`${data.tutor.persona_versions} persona version(s) · ${data.tutor.assigned_students} assigned student(s)`}
        actions={
          <>
            <Badge value={data.tutor.status} />
            <Link href="/tutors">← All tutors</Link>
          </>
        }
      />

      {!active ? (
        <div className="alert alert-warning" role="status" style={{ marginTop: 16 }}>
          No persona version is active. Students assigned to this tutor fall back to the
          default persona until one is activated.
        </div>
      ) : null}

      <h3>Active persona</h3>
      {active ? (
        <>
          {personaField(active.persona, "avatar_url") !== "—" ? (
            <p style={{ margin: "0 0 12px" }}>
              {/* eslint-disable-next-line @next/next/no-img-element -- the URL is
                  operator-supplied and arbitrary; next/image would need every
                  future host allow-listed in next.config to render it at all. */}
              <img
                className="avatar"
                src={personaField(active.persona, "avatar_url")}
                alt={`Avatar for ${personaField(active.persona, "display_name")}`}
              />
            </p>
          ) : null}
          <dl className="kv">
            <dt>Version</dt>
            <dd>
              v{active.version} <Badge value="ACTIVE" />
            </dd>
            <dt>Display name</dt>
            <dd>{personaField(active.persona, "display_name")}</dd>
            <dt>Avatar</dt>
            <dd className="mono">{personaField(active.persona, "avatar_url")}</dd>
            <dt>Subjects</dt>
            <dd>{personaField(active.persona, "subjects")}</dd>
            <dt>Tone</dt>
            <dd>{personaField(active.persona, "tone")}</dd>
            <dt>Pedagogy</dt>
            <dd>{personaField(active.persona, "pedagogy_mode")}</dd>
            <dt>Language</dt>
            <dd>{personaField(active.persona, "language")}</dd>
            <dt>Response style</dt>
            <dd>{personaField(active.persona, "response_style")}</dd>
            <dt>Signature phrases</dt>
            <dd>{personaField(active.persona, "signature_phrases")}</dd>
            <dt>Notifications</dt>
            <dd>
              {personaField(active.persona, "notification_preference")}{" "}
              <span className="badge">stored, not yet acted on</span>
            </dd>
          </dl>
        </>
      ) : (
        <Empty title="No active persona" />
      )}

      <h3>Version history</h3>
      {data.personas.length === 0 ? (
        <Empty title="No persona versions" hint="Draft one below." />
      ) : (
        <DataTable
          caption="Persona versions"
          columns={[
            { key: "version", label: "Version", numeric: true },
            { key: "state", label: "State" },
            { key: "name", label: "Display name" },
            { key: "mode", label: "Pedagogy" },
            { key: "subjects", label: "Subjects" },
            { key: "created", label: "Created" },
            { key: "action", label: "" },
          ]}
        >
          {data.personas.map((persona) => (
            <tr key={persona.id}>
              <td className="numeric">{persona.version}</td>
              <td>
                <Badge value={persona.is_active ? "ACTIVE" : "DRAFT"} />
              </td>
              <td>{personaField(persona.persona, "display_name")}</td>
              <td>{personaField(persona.persona, "pedagogy_mode")}</td>
              <td>{personaField(persona.persona, "subjects")}</td>
              <td>{formatDateTime(persona.created_at)}</td>
              <td>
                {persona.is_active || !can(actor, "persona:activate") ? null : (
                  <details className="risk" style={{ margin: 0 }}>
                    <summary>Activate v{persona.version}</summary>
                    <ActionForm
                      action={activatePersonaAction}
                      submitLabel={`Activate version ${persona.version}`}
                    >
                      <input type="hidden" name="tutor_id" value={data.tutor.id} />
                      <input type="hidden" name="version_id" value={persona.id} />
                      <HighRiskFields
                        actionLabel="changes what every assigned student hears"
                        confirmHint="the previous version is deactivated, not deleted"
                      />
                    </ActionForm>
                  </details>
                )}
              </td>
            </tr>
          ))}
        </DataTable>
      )}

      <h3>Assigned students</h3>
      {data.students.length === 0 ? (
        <Empty
          title="No students assigned"
          hint={mayWrite ? "Assign one below." : undefined}
        />
      ) : (
        <DataTable
          caption="Assigned students"
          columns={[
            { key: "identity", label: "Student" },
            { key: "name", label: "Name" },
            { key: "active", label: "Active" },
            { key: "assigned", label: "Assigned" },
          ]}
        >
          {data.students.map((row) => (
            <tr key={`${row.subject_id}-${row.assigned_at}`}>
              <td>
                <Link href={`/students/${row.subject_id}`}>{row.identity}</Link>
              </td>
              <td>{row.display_name ?? "—"}</td>
              <td>
                <Badge value={row.is_active ? "ACTIVE" : "RETIRED"} />
              </td>
              <td>{formatDateTime(row.assigned_at)}</td>
            </tr>
          ))}
        </DataTable>
      )}

      {mayWrite ? (
        <>
          <h3>Draft a new persona version</h3>
          <fieldset>
            <legend>New draft (inert until activated)</legend>
            <p className="subtitle" style={{ marginBottom: 12 }}>
              Prefilled from{" "}
              {latest ? `version ${latest.version}` : "the defaults"}. Saving creates a new
              version; it changes nothing for a student until it is activated.
            </p>
            <ActionForm action={createPersonaDraftAction} submitLabel="Save draft">
              <input type="hidden" name="tutor_id" value={data.tutor.id} />
              <div className="form-grid">
                <div className="field">
                  <label htmlFor="display_name">Display name</label>
                  <input
                    id="display_name"
                    name="display_name"
                    maxLength={120}
                    defaultValue={personaInput(seed, "display_name") || data.tutor.display_name}
                    required
                  />
                </div>
                <div className="field">
                  <label htmlFor="avatar_url">Avatar URL</label>
                  <input
                    id="avatar_url"
                    name="avatar_url"
                    type="url"
                    maxLength={512}
                    placeholder="https://…"
                    defaultValue={personaInput(seed, "avatar_url")}
                  />
                </div>
                <div className="field">
                  <label htmlFor="subjects">Subjects (comma separated)</label>
                  <input
                    id="subjects"
                    name="subjects"
                    placeholder="maths, physics"
                    defaultValue={personaInput(seed, "subjects")}
                  />
                </div>
                <div className="field">
                  <label htmlFor="tone">Tone</label>
                  <input
                    id="tone"
                    name="tone"
                    maxLength={200}
                    placeholder="warm, direct"
                    defaultValue={personaInput(seed, "tone")}
                  />
                </div>
                <div className="field">
                  <label htmlFor="pedagogy_mode">Default pedagogy mode</label>
                  <select
                    id="pedagogy_mode"
                    name="pedagogy_mode"
                    defaultValue={personaInput(seed, "pedagogy_mode") || "GUIDED"}
                  >
                    {PEDAGOGY_MODES.map((mode) => (
                      <option key={mode} value={mode}>
                        {mode}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label htmlFor="language">Language</label>
                  <input
                    id="language"
                    name="language"
                    maxLength={32}
                    defaultValue={personaInput(seed, "language") || "en"}
                  />
                </div>
                <div className="field">
                  <label htmlFor="notification_preference">Notification preference</label>
                  <select
                    id="notification_preference"
                    name="notification_preference"
                    defaultValue={personaInput(seed, "notification_preference") || "none"}
                  >
                    {NOTIFICATION_PREFERENCES.map((value) => (
                      <option key={value} value={value}>
                        {value}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label htmlFor="signature_phrases">
                    Signature phrases (comma separated)
                  </label>
                  <input
                    id="signature_phrases"
                    name="signature_phrases"
                    placeholder="let us work it out together"
                    defaultValue={personaInput(seed, "signature_phrases")}
                  />
                </div>
                <div className="field span-all">
                  <label htmlFor="response_style">Response style</label>
                  <textarea
                    id="response_style"
                    name="response_style"
                    maxLength={400}
                    defaultValue={personaInput(seed, "response_style")}
                  />
                </div>
              </div>
            </ActionForm>
          </fieldset>

          <h3>Assign a student</h3>
          <fieldset>
            <legend>Assignment</legend>
            <p className="subtitle" style={{ marginBottom: 12 }}>
              One active tutor per student: assigning here retires whatever assignment the
              student had, and the change is written to the audit log.
            </p>
            <ActionForm action={assignStudentAction} submitLabel="Assign student">
              <input type="hidden" name="tutor_id" value={data.tutor.id} />
              <div className="form-grid">
                <div className="field">
                  <label htmlFor="subject_id">Student id</label>
                  {/* A datalist rather than a select: the field still accepts a
                      pasted id (which is how an operator arrives from a support
                      ticket) while offering the known identities to pick from. */}
                  <input
                    id="subject_id"
                    name="subject_id"
                    list="assignable-students"
                    required
                    placeholder="Search identity, or paste an id"
                  />
                  <datalist id="assignable-students">
                    {(students?.items ?? []).map((student) => (
                      <option key={student.id} value={student.id}>
                        {student.external_identity_value}
                        {student.display_name ? ` — ${student.display_name}` : ""}
                      </option>
                    ))}
                  </datalist>
                </div>
                <div className="field">
                  <label htmlFor="reason">Reason</label>
                  <input id="reason" name="reason" required maxLength={500} />
                </div>
              </div>
            </ActionForm>
          </fieldset>
        </>
      ) : null}
    </>
  );
}
