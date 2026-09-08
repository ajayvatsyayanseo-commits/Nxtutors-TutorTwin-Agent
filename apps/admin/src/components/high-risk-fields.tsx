"use client";

import { useId } from "react";

/**
 * The reason box and confirmation tick every dangerous action carries.
 *
 * **The ids are generated, not literal.** A page can hold ten of these — one per
 * kill switch, one per persona version, three on a student — and a hard-coded
 * `id="reason"` makes every one of those labels point at the *first* field on
 * the page. The visible form then looks right while clicking its label focuses
 * something else entirely and assistive technology reads three concatenated
 * labels for one input. `useId` is the reason this is a client component; the
 * fields themselves need no interactivity.
 *
 * **`confirm` is deliberately not `required`.** The API refuses a mutation with
 * no confirmation and no reason, and that refusal is the control — asserted by
 * the security suite, not by this markup. Marking the box `required` would let
 * the browser block the submit with a native bubble that is easy to miss inside
 * a collapsed panel, and would hide the server's own answer. The action returns
 * a sentence saying what is missing instead.
 */
export function HighRiskFields({
  actionLabel,
  confirmHint,
}: {
  actionLabel: string;
  confirmHint?: string;
}) {
  const base = useId();
  const reasonId = `${base}-reason`;
  const hintId = `${base}-reason-hint`;
  const confirmId = `${base}-confirm`;

  return (
    <>
      <div className="field">
        <label htmlFor={reasonId}>
          Reason <span aria-hidden="true">*</span>
        </label>
        <input
          id={reasonId}
          name="reason"
          required
          minLength={8}
          maxLength={500}
          placeholder="Why this change is being made"
          aria-describedby={hintId}
        />
        <span id={hintId} className="hint" style={{ fontSize: 12 }}>
          At least 8 characters. Stored in the audit log against your account.
        </span>
      </div>
      <div className="field">
        <label htmlFor={confirmId} style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <input id={confirmId} name="confirm" type="checkbox" value="yes" />
          <span>
            I understand this {actionLabel}
            {confirmHint ? ` — ${confirmHint}` : ""}.
          </span>
        </label>
      </div>
    </>
  );
}
