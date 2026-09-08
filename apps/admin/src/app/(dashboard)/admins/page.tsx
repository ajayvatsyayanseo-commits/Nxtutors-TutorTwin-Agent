import { ActionForm } from "@/components/action-form";
import { Badge, DataTable, HighRiskFields, PageHeader } from "@/components/ui";
import {
  changeAdminRoleAction,
  createAdminAction,
  setAdminStatusAction,
} from "@/lib/actions";
import { apiFetch } from "@/lib/api";
import { currentActor } from "@/lib/auth-actions";
import { formatDateTime } from "@/lib/format";
import { can } from "@/lib/session";
import type { AdminSummary } from "@/lib/types";

export const metadata = { title: "Administrators · TutorTwin" };
export const dynamic = "force-dynamic";

const ROLES = [
  "SUPER_ADMIN",
  "ADMIN",
  "ACADEMIC_ADMIN",
  "SUPPORT",
  "TUTOR_VIEWER",
  "USAGE_VIEWER",
];

/**
 * Administrator management.
 *
 * Only SUPER_ADMIN may create accounts or change roles. That separation is the
 * point of the role: an account that can grant itself more authority is not a
 * lesser one, and the API refuses regardless of what this page renders —
 * `test_admin_cannot_create_administrators`.
 *
 * A role or status change revokes the target's live sessions, so the change
 * takes effect immediately rather than at the end of their 12-hour session.
 */
export default async function AdminsPage() {
  const [admins, actor] = await Promise.all([
    apiFetch<AdminSummary[]>("/v1/admin/admins"),
    currentActor(),
  ]);

  const writable = can(actor, "admin_user:write");

  return (
    <>
      <PageHeader
        title="Administrators"
        subtitle={
          writable
            ? "You may create accounts and change roles. Every change is audited."
            : "Read-only. Only a super admin can change roles."
        }
      />

      <DataTable
        caption="Administrators"
        columns={[
          { key: "email", label: "Email" },
          { key: "name", label: "Name" },
          { key: "role", label: "Role" },
          { key: "status", label: "Status" },
          { key: "login", label: "Last login" },
          { key: "created", label: "Created" },
          { key: "action", label: "" },
        ]}
      >
        {admins.map((admin) => (
          <tr key={admin.id}>
            <td>{admin.email}</td>
            <td>{admin.display_name || "—"}</td>
            <td>
              <span className="badge">{admin.role}</span>
            </td>
            <td>
              <Badge value={admin.status} />
              {admin.must_change_password ? (
                <span className="badge badge-warn" style={{ marginLeft: 6 }}>
                  must change password
                </span>
              ) : null}
            </td>
            <td>{formatDateTime(admin.last_login_at)}</td>
            <td>{formatDateTime(admin.created_at)}</td>
            <td>
              {writable && admin.id !== actor?.admin_id ? (
                <div className="row-actions">
                  <details className="risk" style={{ margin: 0 }}>
                    <summary>Change role</summary>
                    <ActionForm action={changeAdminRoleAction} submitLabel="Change role">
                      <input type="hidden" name="admin_id" value={admin.id} />
                      <div className="field">
                        <label htmlFor={`role-${admin.id}`}>New role</label>
                        <select id={`role-${admin.id}`} name="role" defaultValue={admin.role}>
                          {ROLES.map((role) => (
                            <option key={role} value={role}>
                              {role}
                            </option>
                          ))}
                        </select>
                      </div>
                      <HighRiskFields
                        actionLabel="changes what this operator can do"
                        confirmHint="their current sessions are revoked"
                      />
                    </ActionForm>
                  </details>
                  <details className="risk" style={{ margin: 0 }}>
                    <summary>{admin.status === "ACTIVE" ? "Disable" : "Enable"}</summary>
                    <ActionForm
                      action={setAdminStatusAction}
                      submitLabel={admin.status === "ACTIVE" ? "Disable account" : "Enable account"}
                      danger={admin.status === "ACTIVE"}
                    >
                      <input type="hidden" name="admin_id" value={admin.id} />
                      <input
                        type="hidden"
                        name="status"
                        value={admin.status === "ACTIVE" ? "DISABLED" : "ACTIVE"}
                      />
                      <HighRiskFields actionLabel="changes whether this operator can sign in" />
                    </ActionForm>
                  </details>
                </div>
              ) : admin.id === actor?.admin_id ? (
                <span className="badge">you</span>
              ) : null}
            </td>
          </tr>
        ))}
      </DataTable>

      {writable ? (
        <>
          <h3>Create an administrator</h3>
          <details className="risk">
            <summary>New administrator account</summary>
            <p style={{ marginTop: 0 }}>
              The password you set here is temporary: the account must change it at first
              login, because a password typed by someone else is not a credential that
              should persist.
            </p>
            <ActionForm action={createAdminAction} submitLabel="Create administrator">
              <div className="field">
                <label htmlFor="email">Email</label>
                <input id="email" name="email" type="email" required />
              </div>
              <div className="field">
                <label htmlFor="display_name">Display name</label>
                <input id="display_name" name="display_name" maxLength={160} />
              </div>
              <div className="field">
                <label htmlFor="role">Role</label>
                <select id="role" name="role" defaultValue="SUPPORT" required>
                  {ROLES.map((role) => (
                    <option key={role} value={role}>
                      {role}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="password">Temporary password</label>
                <input
                  id="password"
                  name="password"
                  type="password"
                  minLength={12}
                  required
                  autoComplete="new-password"
                />
              </div>
              <HighRiskFields actionLabel="grants access to the control plane" />
            </ActionForm>
          </details>
        </>
      ) : null}
    </>
  );
}
