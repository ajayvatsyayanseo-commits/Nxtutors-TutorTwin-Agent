"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";

import { changePasswordAction, type FormState } from "@/lib/auth-actions";

function SubmitButton() {
  const { pending } = useFormStatus();
  return (
    <button type="submit" className="primary" disabled={pending}>
      {pending ? "Saving…" : "Change password"}
    </button>
  );
}

export function ChangePasswordForm() {
  const [state, action] = useActionState<FormState, FormData>(changePasswordAction, {});

  return (
    <form action={action}>
      {state.error ? (
        <div className="alert alert-error" role="alert">
          {state.error}
        </div>
      ) : null}

      <div className="field">
        <label htmlFor="current_password">Current password</label>
        <input
          id="current_password"
          name="current_password"
          type="password"
          autoComplete="current-password"
          required
        />
      </div>

      <div className="field">
        <label htmlFor="new_password">New password</label>
        <input
          id="new_password"
          name="new_password"
          type="password"
          autoComplete="new-password"
          minLength={12}
          required
          aria-describedby="pw-hint"
        />
        <span id="pw-hint" style={{ fontSize: 12, color: "var(--text-muted)" }}>
          At least 12 characters. Length beats punctuation, so a passphrase is fine.
        </span>
      </div>

      <div className="field">
        <label htmlFor="confirm_password">Confirm new password</label>
        <input
          id="confirm_password"
          name="confirm_password"
          type="password"
          autoComplete="new-password"
          minLength={12}
          required
        />
      </div>

      <SubmitButton />
    </form>
  );
}
