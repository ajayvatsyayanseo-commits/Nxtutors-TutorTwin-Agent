import { ActionForm } from "@/components/action-form";
import { BoolBadge, DataTable, HighRiskFields, PageHeader } from "@/components/ui";
import { setFlagAction } from "@/lib/actions";
import { apiFetch } from "@/lib/api";
import { currentActor } from "@/lib/auth-actions";
import { formatDateTime } from "@/lib/format";
import { can } from "@/lib/session";
import type { FlagView } from "@/lib/types";

export const metadata = { title: "Feature flags · TutorTwin" };
export const dynamic = "force-dynamic";

/**
 * Kill switches.
 *
 * Every known switch is listed even when no row exists yet, defaulting to
 * enabled. A switch missing from the list because nobody had toggled it would
 * look like a switch that does not exist, which is the opposite of what a kill
 * switch is for — you reach for it during an incident, not before one.
 *
 * The API refuses an unknown key: inventing one would create a control that
 * nothing reads, which is worse than no control because it looks like it worked.
 */
export default async function FlagsPage() {
  const [flags, actor] = await Promise.all([
    apiFetch<FlagView[]>("/v1/admin/flags"),
    currentActor(),
  ]);

  const writable = can(actor, "flag:write");
  const disabled = flags.filter((flag) => !flag.enabled);

  return (
    <>
      <PageHeader
        title="Feature flags"
        subtitle="Disabling a capability takes effect on the next request. No deploy required."
      />

      {disabled.length > 0 ? (
        <div className="alert alert-warning" role="status">
          <strong>{disabled.length} capability disabled:</strong>{" "}
          {disabled.map((flag) => flag.key).join(", ")}
        </div>
      ) : null}

      <DataTable
        caption="Feature flags"
        columns={[
          { key: "key", label: "Switch" },
          { key: "description", label: "Controls", wrap: true },
          { key: "state", label: "State" },
          { key: "updated", label: "Changed" },
          { key: "action", label: "" },
        ]}
      >
        {flags.map((flag) => (
          <tr key={flag.key}>
            <td className="mono">{flag.key}</td>
            <td className="wrap">{flag.description ?? "—"}</td>
            <td>
              <BoolBadge value={flag.enabled} on="enabled" off="disabled" />
            </td>
            <td>{formatDateTime(flag.updated_at)}</td>
            <td>
              {writable ? (
                <details className="risk" style={{ margin: 0 }}>
                  <summary>{flag.enabled ? "Disable" : "Enable"}</summary>
                  <ActionForm
                    action={setFlagAction}
                    submitLabel={flag.enabled ? `Disable ${flag.key}` : `Enable ${flag.key}`}
                    danger={flag.enabled}
                  >
                    <input type="hidden" name="key" value={flag.key} />
                    <input
                      type="hidden"
                      name="enabled"
                      value={flag.enabled ? "no" : "yes"}
                    />
                    <HighRiskFields
                      actionLabel={
                        flag.enabled
                          ? "stops this capability for every student"
                          : "re-enables this capability for every student"
                      }
                    />
                  </ActionForm>
                </details>
              ) : null}
            </td>
          </tr>
        ))}
      </DataTable>
    </>
  );
}
