"use client";

import { useEffect, useState } from "react";

import { API_URL } from "@/lib/config";

/**
 * Polls the server for the real outcome of an order.
 *
 * Cashfree's webhook is the authority and usually lands first, but it is a
 * separate network path and can be seconds behind the student's browser. So
 * this polls a server endpoint that, if the webhook has not arrived, asks
 * Cashfree directly and activates from that. The student gets a definite
 * answer either way rather than a spinner and a guess.
 *
 * Polling stops on a definite result and after a bounded number of attempts,
 * because a page left open on a phone should not keep hitting the API all
 * afternoon.
 */

type State = "checking" | "active" | "pending" | "failed";

const INTERVAL_MS = 2500;
const MAX_ATTEMPTS = 16; // ~40 seconds, then we stop and explain.

export function ActivationWatcher({ orderId }: { orderId: string }) {
  const [state, setState] = useState<State>("checking");

  useEffect(() => {
    let attempts = 0;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;

    async function poll() {
      attempts += 1;
      try {
        const response = await fetch(
          `${API_URL}/public/orders/${encodeURIComponent(orderId)}`,
          { cache: "no-store" },
        );
        if (response.ok) {
          const body = (await response.json()) as { status: string; activated: boolean };
          if (cancelled) return;

          if (body.activated) {
            setState("active");
            return;
          }
          if (["FAILED", "USER_DROPPED", "CANCELLED"].includes(body.status)) {
            setState("failed");
            return;
          }
        }
      } catch {
        // A transient network error is not an answer. Fall through and retry.
      }

      if (cancelled) return;
      if (attempts >= MAX_ATTEMPTS) {
        setState("pending");
        return;
      }
      timer = setTimeout(poll, INTERVAL_MS);
    }

    void poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [orderId]);

  if (state === "checking") {
    return (
      <>
        <div
          className="spinner spinner--dark"
          style={{ width: 34, height: 34, borderWidth: 3, margin: "0 auto 1.5rem" }}
          aria-hidden="true"
        />
        <h2>Confirming your payment</h2>
        <p className="muted" style={{ marginTop: "1rem" }}>
          This takes a few seconds. Please do not close this page.
        </p>
      </>
    );
  }

  if (state === "active") {
    return (
      <>
        <div style={{ fontSize: "3.5rem", lineHeight: 1 }} aria-hidden="true">
          🎉
        </div>
        <h2 style={{ marginTop: "1rem" }}>Your tutor is live</h2>
        <p className="notice notice--ok" style={{ marginTop: "1.5rem", textAlign: "left" }}>
          We have sent a message to your WhatsApp number. Open WhatsApp and
          reply to it - you can start asking questions straight away.
        </p>
        <p className="muted">
          Send a photo of any question, a worksheet PDF, or just type what you
          are stuck on.
        </p>
      </>
    );
  }

  if (state === "failed") {
    return (
      <>
        <h2>That payment did not go through</h2>
        <p className="muted" style={{ marginTop: "1rem" }}>
          Nothing has been charged. You can try again - your details are not
          saved to the payment page, so you will need to fill the short form
          once more.
        </p>
        <a className="btn" href="/payment" style={{ marginTop: "1.75rem" }}>
          Try again
        </a>
      </>
    );
  }

  return (
    <>
      <h2>Still confirming</h2>
      <p className="muted" style={{ marginTop: "1rem" }}>
        Your payment is taking longer than usual to confirm. If money has left
        your account, your subscription will activate automatically and your
        tutor will message you on WhatsApp - you do not need to pay again or
        keep this page open.
      </p>
      <p className="muted" style={{ marginTop: "1rem", fontSize: "0.86rem" }}>
        Reference: <code>{orderId}</code>
      </p>
      <p className="muted" style={{ marginTop: "1rem", fontSize: "0.86rem" }}>
        Still nothing after ten minutes? Email{" "}
        <a href="mailto:tutortwin@nxturors.in">tutortwin@nxturors.in</a> with
        that reference.
      </p>
    </>
  );
}
