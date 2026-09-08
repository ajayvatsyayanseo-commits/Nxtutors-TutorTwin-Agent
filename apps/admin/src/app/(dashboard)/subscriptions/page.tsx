import { ActionForm } from "@/components/action-form";
import { Empty, HighRiskFields, Notice, PageHeader } from "@/components/ui";
import { grantSubscriptionAction } from "@/lib/actions";
import { currentActor } from "@/lib/auth-actions";
import { can } from "@/lib/session";

export const metadata = { title: "Grant subscription · TutorTwin" };
export const dynamic = "force-dynamic";

/**
 * Give somebody a subscription without a payment.
 *
 * Keyed on the **WhatsApp number**, not on a student id, because the whole
 * point is granting access to a person who has never used the product and
 * therefore has no student row yet. The service creates the identity, the
 * tutor persona and the entitlement together.
 *
 * It writes through exactly the same path as a paid activation - same tables,
 * same supersede rule, same WhatsApp template - so a comped subscription
 * behaves identically to a bought one everywhere downstream. Only `source`
 * differs, which is what tells an operator later that no money was involved.
 */
export default async function SubscriptionsPage() {
  const actor = await currentActor();

  if (!can(actor, "student:write")) {
    return (
      <>
        <PageHeader
          title="Grant subscription"
          subtitle="Give a student access without a payment."
        />
        <Empty
          title="Your role cannot grant subscriptions"
          hint="This needs the student:write permission. Ask a super admin."
        />
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Grant subscription"
        subtitle="Give a student access without a payment. Recorded in the audit log against your account."
      />

      <Notice kind="warning">
        This grants paid access and, unless you untick it below, sends the
        student a WhatsApp message telling them their tutor is live. Check the
        number before submitting - a typo grants a subscription to a stranger.
      </Notice>

      <ActionForm action={grantSubscriptionAction} submitLabel="Grant subscription">
        <div className="field">
          <label htmlFor="whatsapp_number">
            WhatsApp number <span aria-hidden="true">*</span>
          </label>
          <input
            id="whatsapp_number"
            name="whatsapp_number"
            required
            inputMode="tel"
            maxLength={32}
            placeholder="9999000001"
          />
          <span className="hint" style={{ fontSize: 12 }}>
            With or without +91 - it is normalised. This is the identity the
            agent runs on, and where the notification goes.
          </span>
        </div>

        <div className="field">
          <label htmlFor="student_name">
            Student name <span aria-hidden="true">*</span>
          </label>
          <input
            id="student_name"
            name="student_name"
            required
            maxLength={200}
            placeholder="Aarav Sharma"
          />
        </div>

        <div className="field">
          <label htmlFor="tutor_name">Tutor name</label>
          <input
            id="tutor_name"
            name="tutor_name"
            maxLength={200}
            defaultValue="TutorTwin"
            placeholder="Anita Ma'am"
          />
          <span className="hint" style={{ fontSize: 12 }}>
            What the agent answers to. It never claims to be that person.
          </span>
        </div>

        <div className="field">
          <label htmlFor="subject">Main subject</label>
          <input
            id="subject"
            name="subject"
            maxLength={120}
            defaultValue="General"
            placeholder="Physics"
          />
        </div>

        <div className="field">
          <label htmlFor="plan_code">Plan</label>
          <input id="plan_code" name="plan_code" maxLength={64} defaultValue="PRO" />
          <span className="hint" style={{ fontSize: 12 }}>
            Must match a plan policy, or the budget gate falls back to defaults.
          </span>
        </div>

        <div className="field">
          <label htmlFor="days">Days</label>
          <input
            id="days"
            name="days"
            type="number"
            min={1}
            max={3660}
            defaultValue={30}
          />
          <span className="hint" style={{ fontSize: 12 }}>
            Access ends after this many days. Expiry is enforced on every
            request, not by a nightly job.
          </span>
        </div>

        <div className="field">
          <label htmlFor="notify" style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <input
              id="notify"
              name="notify"
              type="checkbox"
              value="yes"
              defaultChecked
            />
            <span>
              Send a WhatsApp message telling them it is active
            </span>
          </label>
          <span className="hint" style={{ fontSize: 12 }}>
            Leave ticked unless you are correcting a mistake. A student who is
            not told they have a subscription behaves exactly like one who does
            not have it.
          </span>
        </div>

        <HighRiskFields
          actionLabel="grants paid access"
          confirmHint="it costs money and messages the student"
        />
      </ActionForm>
    </>
  );
}
