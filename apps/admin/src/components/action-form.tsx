"use client";

import { startTransition, useActionState } from "react";
import type { FormEvent, ReactNode } from "react";

import type { FormState } from "@/lib/auth-actions";

/**
 * The wrapper every mutation form uses.
 *
 * It gives each one the same three things: a pending state so the operator
 * cannot double-submit a dangerous action, an inline error that keeps their
 * input, and a success message that says what actually happened.
 *
 * There is deliberately **no optimistic update** on any of these. Every action
 * here changes cost, entitlement or routing; showing success before the server
 * has agreed would be a lie exactly when a lie is most expensive. Optimism is
 * reserved for things that cannot fail meaningfully, and none of these qualify.
 *
 * **The submit is dispatched by hand rather than through `<form action={...}>`.**
 * React resets an uncontrolled form once its action returns, so the direct form
 * would blank the reason an operator had just typed every time the server
 * refused the change — and a reason that has to be typed twice becomes "asdf"
 * on the second attempt. Building the `FormData` in the submit handler keeps the
 * fields exactly as they were. Native validation still runs first: the browser
 * does not fire `submit` at all on an invalid form.
 */
export function ActionForm({
  action,
  submitLabel,
  danger = false,
  children,
}: {
  action: (state: FormState, formData: FormData) => Promise<FormState>;
  submitLabel: string;
  danger?: boolean;
  children: ReactNode;
}) {
  const [state, dispatch, pending] = useActionState<FormState, FormData>(action, {});

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    // Read synchronously: the form element is not safe to touch inside the
    // transition callback.
    const data = new FormData(event.currentTarget);
    startTransition(() => dispatch(data));
  }

  return (
    <form onSubmit={onSubmit}>
      {state.error ? (
        <div className="alert alert-error" role="alert">
          {state.error}
        </div>
      ) : null}
      {state.success ? (
        <div className="alert alert-success" role="status">
          {state.success}
        </div>
      ) : null}
      {children}
      <button
        type="submit"
        className={danger ? "danger" : "primary"}
        disabled={pending}
        aria-busy={pending}
      >
        {pending ? "Working…" : submitLabel}
      </button>
    </form>
  );
}
