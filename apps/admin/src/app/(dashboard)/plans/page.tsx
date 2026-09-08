import Link from "next/link";

import { ActionForm } from "@/components/action-form";
import { BoolBadge, DataTable, Empty, HighRiskFields, PageHeader } from "@/components/ui";
import { upsertPlanAction } from "@/lib/actions";
import { apiFetch } from "@/lib/api";
import { currentActor } from "@/lib/auth-actions";
import { formatDateTime } from "@/lib/format";
import { can } from "@/lib/session";
import type { PlanView } from "@/lib/types";

export const metadata = { title: "Plans · TutorTwin" };
export const dynamic = "force-dynamic";

const LIMIT_TEMPLATE = `{
  "daily_model_calls": 40,
  "monthly_model_calls": 800,
  "daily_budget_micros": 200000,
  "max_file_mb": 10,
  "max_pdf_pages": 20,
  "max_audio_seconds": 120,
  "max_mock_minutes": 30,
  "max_mock_questions": 10,
  "verifier_allowance": 5,
  "retention_days": 30
}`;

/**
 * Plan policies.
 *
 * **Editing publishes a new version; it never rewrites the old one.** A request
 * refused last Tuesday must still be explicable by the policy that refused it,
 * and an in-place edit destroys exactly that evidence.
 */
export default async function PlansPage({
  searchParams,
}: {
  searchParams: Promise<{ history?: string }>;
}) {
  const params = await searchParams;
  const includeHistory = params.history === "yes";

  const [plans, actor] = await Promise.all([
    apiFetch<PlanView[]>("/v1/admin/plans", { query: { include_history: includeHistory } }),
    currentActor(),
  ]);

  return (
    <>
      <PageHeader
        title="Plans & entitlements"
        subtitle="Feature matrix, quotas and limits. Every change publishes a new version."
        actions={
          <Link href={includeHistory ? "/plans" : "/plans?history=yes"}>
            {includeHistory ? "Latest only" : "Show all versions"}
          </Link>
        }
      />

      {plans.length === 0 ? (
        <Empty
          title="No plan policies"
          hint="Without a policy the budget gate falls back to its code defaults."
        />
      ) : (
        <DataTable
          caption="Plan policies"
          columns={[
            { key: "plan", label: "Plan" },
            { key: "version", label: "Version", numeric: true },
            { key: "paid", label: "Paid AI" },
            { key: "limits", label: "Limits", wrap: true },
            { key: "features", label: "Features", wrap: true },
            { key: "created", label: "Published" },
          ]}
        >
          {plans.map((plan) => (
            <tr key={`${plan.plan_code}-${plan.version}`}>
              <td>{plan.plan_code}</td>
              <td className="numeric">{plan.version}</td>
              <td>
                <BoolBadge value={plan.allows_paid_ai} on="allowed" off="blocked" />
              </td>
              <td className="wrap mono">{JSON.stringify(plan.limits)}</td>
              <td className="wrap mono">{JSON.stringify(plan.features)}</td>
              <td>{formatDateTime(plan.created_at)}</td>
            </tr>
          ))}
        </DataTable>
      )}

      {can(actor, "plan:write") ? (
        <>
          <h3>Publish a plan version</h3>
          <details className="risk">
            <summary>Change plan limits</summary>
            <p style={{ marginTop: 0 }}>
              This changes what every student on the plan may do and spend. The previous
              version is retained so past decisions stay explicable.
            </p>
            <ActionForm action={upsertPlanAction} submitLabel="Publish version">
              <div className="field">
                <label htmlFor="plan_code">Plan code</label>
                <input id="plan_code" name="plan_code" required maxLength={64} />
              </div>
              <div className="field">
                <label htmlFor="allows_paid_ai" style={{ display: "flex", gap: 8 }}>
                  <input
                    id="allows_paid_ai"
                    name="allows_paid_ai"
                    type="checkbox"
                    value="yes"
                  />
                  <span>Allows paid AI calls</span>
                </label>
              </div>
              <div className="field">
                <label htmlFor="limits">Limits (JSON)</label>
                <textarea
                  id="limits"
                  name="limits"
                  defaultValue={LIMIT_TEMPLATE}
                  rows={12}
                  aria-describedby="limits-hint"
                />
                <span id="limits-hint" style={{ fontSize: 12, color: "var(--text-muted)" }}>
                  Quotas, file and page caps, mock ceilings, provider budget, verifier
                  allowance and retention.
                </span>
              </div>
              <div className="field">
                <label htmlFor="features">Features (JSON)</label>
                <textarea id="features" name="features" defaultValue="{}" rows={5} />
              </div>
              <HighRiskFields actionLabel="changes limits for every student on this plan" />
            </ActionForm>
          </details>
        </>
      ) : null}
    </>
  );
}
