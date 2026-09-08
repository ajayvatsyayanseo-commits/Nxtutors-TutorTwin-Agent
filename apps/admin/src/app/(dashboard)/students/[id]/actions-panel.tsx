import { ActionForm } from "@/components/action-form";
import { HighRiskFields } from "@/components/ui";
import {
  deleteStudentDataAction,
  overrideEntitlementAction,
  resetQuotaAction,
} from "@/lib/actions";

/**
 * The dangerous half of the student page.
 *
 * Each action is collapsed behind a `<details>` so it cannot be triggered by a
 * stray click on a page an operator opened to read. Deleting data additionally
 * requires typing the student's identity: a mis-clicked delete on the wrong row
 * is unrecoverable, so the target has to be named, and the API checks the typed
 * value against the row it is about to erase.
 */
export function StudentActions({
  studentId,
  identity,
  currentPlan,
}: {
  studentId: string;
  identity: string;
  currentPlan: string;
}) {
  return (
    <>
      <details className="risk">
        <summary>Override entitlement</summary>
        <p style={{ marginTop: 0 }}>
          Creates a local override marked <code>admin_override</code>, so a later refresh
          from the website can be told apart from an operator decision.
        </p>
        <ActionForm action={overrideEntitlementAction} submitLabel="Override entitlement">
          <input type="hidden" name="student_id" value={studentId} />
          <div className="field">
            <label htmlFor="plan_code">Plan code</label>
            <input id="plan_code" name="plan_code" defaultValue={currentPlan} required />
          </div>
          <div className="field">
            <label htmlFor="status">Status</label>
            <select id="status" name="status" defaultValue="ACTIVE">
              <option value="ACTIVE">ACTIVE</option>
              <option value="INACTIVE">INACTIVE</option>
              <option value="SUSPENDED">SUSPENDED</option>
            </select>
          </div>
          <HighRiskFields
            actionLabel="changes what this student may use"
            confirmHint="it takes effect on their next message"
          />
        </ActionForm>
      </details>

      <details className="risk">
        <summary>Reset daily quota</summary>
        <p style={{ marginTop: 0 }}>
          Records a new starting point for the budgeter. Spend history in the usage
          ledger is cost evidence and is never deleted.
        </p>
        <ActionForm action={resetQuotaAction} submitLabel="Reset quota">
          <input type="hidden" name="student_id" value={studentId} />
          <HighRiskFields actionLabel="grants further paid usage today" />
        </ActionForm>
      </details>

      <details className="risk">
        <summary>Delete learning data</summary>
        <p style={{ marginTop: 0 }}>
          Erases memories, progress, documents, assessments, decks, artifacts and
          media. The identity row and the audit record of this deletion are kept —
          a record that someone deleted a student&apos;s data is not itself student data.
        </p>
        <ActionForm
          action={deleteStudentDataAction}
          submitLabel="Delete learning data"
          danger
        >
          <input type="hidden" name="student_id" value={studentId} />
          <div className="field">
            <label htmlFor="confirm_identity">
              Type the student identity to confirm: <code>{identity}</code>
            </label>
            <input id="confirm_identity" name="confirm_identity" required autoComplete="off" />
          </div>
          <HighRiskFields
            actionLabel="permanently deletes this student's learning data"
            confirmHint="it cannot be undone"
          />
        </ActionForm>
      </details>
    </>
  );
}
