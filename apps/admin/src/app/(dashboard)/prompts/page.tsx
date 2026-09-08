import { ActionForm } from "@/components/action-form";
import { Badge, DataTable, Empty, HighRiskFields, PageHeader } from "@/components/ui";
import { activatePromptAction, createPromptDraftAction } from "@/lib/actions";
import { apiFetch } from "@/lib/api";
import { currentActor } from "@/lib/auth-actions";
import { formatDateTime, truncate } from "@/lib/format";
import { can } from "@/lib/session";
import type { PromptVersionView } from "@/lib/types";

export const metadata = { title: "Prompt versions · TutorTwin" };
export const dynamic = "force-dynamic";

/**
 * Prompt versions.
 *
 * Immutable history: activating a version retires the previous one rather than
 * overwriting it, and **rollback is the same operation applied to an older
 * version**. There is no separate rollback path to get wrong.
 *
 * The diff shown is the previous active body beside the new one. Nothing here
 * renders untrusted markup — prompt bodies are displayed as text.
 */
export default async function PromptsPage({
  searchParams,
}: {
  searchParams: Promise<{ block_key?: string }>;
}) {
  const params = await searchParams;
  const [versions, actor] = await Promise.all([
    apiFetch<PromptVersionView[]>("/v1/admin/prompts", {
      query: { block_key: params.block_key },
    }),
    currentActor(),
  ]);

  const writable = can(actor, "prompt:write");
  const byBlock = new Map<string, PromptVersionView[]>();
  for (const version of versions) {
    const list = byBlock.get(version.block_key) ?? [];
    list.push(version);
    byBlock.set(version.block_key, list);
  }

  return (
    <>
      <PageHeader
        title="Prompt versions"
        subtitle="Draft, activate, roll back. An activated version is never edited in place."
      />

      {versions.length === 0 ? (
        <Empty
          title="No prompt overrides"
          hint="The engine is running the prompt blocks compiled into the service."
        />
      ) : (
        [...byBlock.entries()].map(([blockKey, rows]) => (
          <section key={blockKey}>
            <h3>{blockKey}</h3>
            <DataTable
              caption={`Versions of ${blockKey}`}
              columns={[
                { key: "version", label: "Version", numeric: true },
                { key: "status", label: "Status" },
                { key: "body", label: "Body", wrap: true },
                { key: "reason", label: "Reason", wrap: true },
                { key: "created", label: "Created" },
                { key: "action", label: "" },
              ]}
            >
              {rows.map((version) => (
                <tr key={version.id}>
                  <td className="numeric">{version.version}</td>
                  <td>
                    <Badge value={version.status} />
                  </td>
                  <td className="wrap mono">{truncate(version.body, 220)}</td>
                  <td className="wrap">{version.reason ?? "—"}</td>
                  <td>{formatDateTime(version.created_at)}</td>
                  <td>
                    {version.status === "ACTIVE" || !writable ? null : (
                      <details className="risk" style={{ margin: 0 }}>
                        <summary>
                          {version.status === "RETIRED" ? "Roll back to" : "Activate"} v
                          {version.version}
                        </summary>
                        <ActionForm
                          action={activatePromptAction}
                          submitLabel={`Activate version ${version.version}`}
                        >
                          <input type="hidden" name="version_id" value={version.id} />
                          <HighRiskFields
                            actionLabel="changes the prompt every student's answer is built from"
                          />
                        </ActionForm>
                      </details>
                    )}
                  </td>
                </tr>
              ))}
            </DataTable>
          </section>
        ))
      )}

      {writable ? (
        <>
          <h3>New draft</h3>
          <fieldset>
            <legend>Draft a prompt version (inert until activated)</legend>
            <ActionForm action={createPromptDraftAction} submitLabel="Save draft">
              <div className="field">
                <label htmlFor="block_key">Block key</label>
                <input
                  id="block_key"
                  name="block_key"
                  required
                  maxLength={64}
                  placeholder="persona_preamble"
                />
              </div>
              <div className="field">
                <label htmlFor="body">Body</label>
                <textarea id="body" name="body" required rows={10} maxLength={8000} />
              </div>
              <div className="field">
                <label htmlFor="reason">Reason</label>
                <input id="reason" name="reason" required maxLength={500} />
              </div>
            </ActionForm>
          </fieldset>
        </>
      ) : null}
    </>
  );
}
