"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";

import { loginAction, type FormState } from "@/lib/auth-actions";

function SubmitButton() {
  const { pending } = useFormStatus();
  return (
    <button type="submit" className="primary" disabled={pending}>
      {pending ? "Signing in…" : "Sign in"}
    </button>
  );
}

/**
 * The login form.
 *
 * The password never leaves this request: it is posted to a server action, which
 * calls the API server-side. Nothing is stored in `localStorage`, and no token
 * reaches the client — a page compromised by an injected script has nothing to
 * read, because the credential lives in an httpOnly cookie.
 */
export function LoginForm() {
  const [state, action] = useActionState<FormState, FormData>(loginAction, {});

  return (
    <form action={action} noValidate={false}>
      {state.error ? (
        <div className="alert alert-error" role="alert">
          {state.error}
        </div>
      ) : null}

      <div className="field">
        <label htmlFor="email">Email</label>
        <input
          id="email"
          name="email"
          type="email"
          autoComplete="username"
          required
          autoFocus
          aria-invalid={state.error ? true : undefined}
        />
      </div>

      <div className="field">
        <label htmlFor="password">Password</label>
        <input
          id="password"
          name="password"
          type="password"
          autoComplete="current-password"
          required
          aria-invalid={state.error ? true : undefined}
        />
      </div>

      <SubmitButton />
    </form>
  );
}
