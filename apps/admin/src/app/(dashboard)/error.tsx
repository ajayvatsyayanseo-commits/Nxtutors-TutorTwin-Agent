"use client";

import { useEffect } from "react";

/**
 * The error boundary every dashboard page falls back to.
 *
 * It shows what went wrong in operator terms and offers a retry, because the
 * most common cause is the API being briefly unreachable rather than a bug.
 *
 * The `digest` is Next's server-side error id. The message itself is not shown
 * for a server error — Next redacts it in production precisely so a stack trace
 * or a connection string cannot reach a browser — so the digest is what a
 * developer correlates against the server log.
 */
export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("control-plane error", error.digest ?? error.message);
  }, [error]);

  return (
    <div className="alert alert-error" role="alert">
      <strong>Something went wrong loading this page.</strong>
      <p style={{ margin: "8px 0" }}>
        The TutorTwin API may be unreachable, or the request may have timed out.
      </p>
      {error.digest ? (
        <p className="mono" style={{ margin: "0 0 12px" }}>
          Reference: {error.digest}
        </p>
      ) : null}
      <button type="button" onClick={reset}>
        Try again
      </button>
    </div>
  );
}
