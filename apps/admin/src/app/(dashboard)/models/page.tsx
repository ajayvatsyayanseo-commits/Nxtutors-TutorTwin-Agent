import { ActionForm } from "@/components/action-form";
import { BoolBadge, DataTable, Empty, HighRiskFields, PageHeader } from "@/components/ui";
import { upsertModelRouteAction } from "@/lib/actions";
import { apiFetch } from "@/lib/api";
import { currentActor } from "@/lib/auth-actions";
import { formatDateTime, formatMicros } from "@/lib/format";
import { can } from "@/lib/session";
import type { ModelCatalogResponse } from "@/lib/types";

export const metadata = { title: "Models & routing · TutorTwin" };
export const dynamic = "force-dynamic";

/**
 * Model routing.
 *
 * **No secret value appears anywhere on this page, and none is available to
 * appear.** API keys live in the environment; the catalog holds an alias, a
 * vendor model id and a price. `provider_key_configured` is a boolean — whether
 * a credential exists — which is the only fact an operator needs and the only
 * one that is safe to send. Asserted by
 * `test_model_catalog_reports_key_presence_not_key_value`.
 */
export default async function ModelsPage() {
  const [data, actor] = await Promise.all([
    apiFetch<ModelCatalogResponse>("/v1/admin/models"),
    currentActor(),
  ]);

  return (
    <>
      <PageHeader
        title="Models & routing"
        subtitle="Alias → provider → model id. Business code never names a vendor model."
      />

      <h3>Provider credentials</h3>
      <div className="grid">
        {Object.entries(data.provider_key_configured).map(([provider, configured]) => (
          <div key={provider} className="card">
            <div className="label">{provider}</div>
            <div className="value" style={{ fontSize: 18 }}>
              <BoolBadge value={configured} on="key configured" off="no key" />
            </div>
            <div className="hint">
              Presence only. The value is never sent to this page.
            </div>
          </div>
        ))}
      </div>

      <h3>Routes</h3>
      {data.routes.length === 0 ? (
        <Empty
          title="No routes configured"
          hint="Without a route the gateway has no model for an alias and the request degrades rather than failing."
        />
      ) : (
        <DataTable
          caption="Model routes"
          columns={[
            { key: "alias", label: "Alias" },
            { key: "provider", label: "Provider" },
            { key: "model", label: "Model id" },
            { key: "active", label: "Active" },
            { key: "in", label: "Input / 1k", numeric: true },
            { key: "out", label: "Output / 1k", numeric: true },
            { key: "rate", label: "Rate version" },
            { key: "created", label: "Created" },
          ]}
        >
          {data.routes.map((route) => (
            <tr key={route.id}>
              <td>{route.model_alias}</td>
              <td>{route.provider}</td>
              <td className="mono">{route.model_id}</td>
              <td>
                <BoolBadge value={route.is_active} />
              </td>
              <td className="numeric">{formatMicros(route.input_cost_micros_per_1k)}</td>
              <td className="numeric">{formatMicros(route.output_cost_micros_per_1k)}</td>
              <td>{route.rate_version}</td>
              <td>{formatDateTime(route.created_at)}</td>
            </tr>
          ))}
        </DataTable>
      )}

      {can(actor, "model:write") ? (
        <>
          <h3>Add or update a route</h3>
          <details className="risk">
            <summary>Change model routing</summary>
            <p style={{ marginTop: 0 }}>
              This is where money moves. Existing ledger rows keep the rate that applied
              when they were written; only new calls use the price set here.
            </p>
            <ActionForm action={upsertModelRouteAction} submitLabel="Save route">
              <div className="field">
                <label htmlFor="model_alias">Alias</label>
                <select id="model_alias" name="model_alias" required>
                  {data.aliases.map((alias) => (
                    <option key={alias} value={alias}>
                      {alias}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="provider">Provider</label>
                <select id="provider" name="provider" required defaultValue="OPENAI">
                  <option value="OPENAI">OPENAI</option>
                  <option value="ANTHROPIC">ANTHROPIC</option>
                  <option value="FAKE">FAKE</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="model_id">Vendor model id</label>
                <input id="model_id" name="model_id" required maxLength={128} />
              </div>
              <div className="field">
                <label htmlFor="input_cost">Input cost, micros per 1k tokens</label>
                <input
                  id="input_cost"
                  name="input_cost"
                  type="number"
                  min={0}
                  max={10000000}
                  defaultValue={0}
                  required
                />
              </div>
              <div className="field">
                <label htmlFor="output_cost">Output cost, micros per 1k tokens</label>
                <input
                  id="output_cost"
                  name="output_cost"
                  type="number"
                  min={0}
                  max={10000000}
                  defaultValue={0}
                  required
                />
              </div>
              <div className="field">
                <label htmlFor="rate_version">Rate version</label>
                <input id="rate_version" name="rate_version" defaultValue="v1" required />
              </div>
              <div className="field">
                <label htmlFor="is_active" style={{ display: "flex", gap: 8 }}>
                  <input
                    id="is_active"
                    name="is_active"
                    type="checkbox"
                    value="yes"
                    defaultChecked
                  />
                  <span>Route is active</span>
                </label>
              </div>
              <HighRiskFields
                actionLabel="changes which model answers students and what it costs"
              />
            </ActionForm>
          </details>
        </>
      ) : null}
    </>
  );
}
