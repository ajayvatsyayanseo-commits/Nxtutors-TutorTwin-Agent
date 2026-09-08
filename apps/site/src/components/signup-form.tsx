"use client";

import { useEffect, useState } from "react";

import { API_URL, CASHFREE_MODE } from "@/lib/config";

/**
 * The signup form, and the handoff to Cashfree.
 *
 * The order is created **server-side** and this component only ever receives a
 * `payment_session_id`. It never sees the amount as something it could change,
 * and it never reports success - a browser saying "I paid" is not evidence, so
 * the entitlement is granted by the signed webhook and confirmed by a
 * server-side status check on the success page.
 */

interface CashfreeCheckout {
  checkout: (options: {
    paymentSessionId: string;
    redirectTarget?: "_self" | "_blank" | "_modal";
  }) => Promise<{ error?: { message?: string } }>;
}

declare global {
  interface Window {
    Cashfree?: (config: { mode: "sandbox" | "production" }) => CashfreeCheckout;
  }
}

const SDK_SRC = "https://sdk.cashfree.com/js/v3/cashfree.js";

type Status = "idle" | "submitting" | "redirecting" | "error";

export function SignupForm() {
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<string | null>(null);
  const [sdkReady, setSdkReady] = useState(false);

  // Loaded here rather than in the page head so it is fetched only by people
  // who actually reach the form, and so a CDN outage degrades to a clear
  // message instead of a checkout button that silently does nothing.
  useEffect(() => {
    if (window.Cashfree) {
      setSdkReady(true);
      return;
    }
    const script = document.createElement("script");
    script.src = SDK_SRC;
    script.async = true;
    script.onload = () => setSdkReady(true);
    script.onerror = () =>
      setError("The payment library could not load. Check your connection and refresh.");
    document.body.appendChild(script);
  }, []);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setStatus("submitting");

    const data = new FormData(event.currentTarget);
    const payload = {
      student_name: String(data.get("student_name") ?? "").trim(),
      whatsapp_number: String(data.get("whatsapp_number") ?? "").trim(),
      contact_phone: String(data.get("contact_phone") ?? "").trim() || null,
      tutor_name: String(data.get("tutor_name") ?? "").trim(),
      subject: String(data.get("subject") ?? "").trim(),
      location: String(data.get("location") ?? "").trim() || null,
    };

    let sessionId: string;
    try {
      const response = await fetch(`${API_URL}/public/signup`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const body: unknown = await response.json().catch(() => null);
        const detail =
          body && typeof body === "object" && "message" in body
            ? String((body as { message: unknown }).message)
            : "We could not start the payment. Please try again.";
        throw new Error(detail);
      }
      const body = (await response.json()) as { payment_session_id: string };
      sessionId = body.payment_session_id;
    } catch (cause) {
      setStatus("error");
      setError(cause instanceof Error ? cause.message : "Something went wrong.");
      return;
    }

    if (!window.Cashfree) {
      setStatus("error");
      setError("The payment library is still loading. Give it a second and try again.");
      return;
    }

    setStatus("redirecting");
    const cashfree = window.Cashfree({ mode: CASHFREE_MODE });
    const result = await cashfree.checkout({
      paymentSessionId: sessionId,
      // Same tab. A popup is blocked by default on most mobile browsers, which
      // is exactly where this form is filled in.
      redirectTarget: "_self",
    });

    if (result?.error) {
      setStatus("error");
      setError(result.error.message ?? "The payment could not be opened.");
    }
  }

  const busy = status === "submitting" || status === "redirecting";

  return (
    <form className="panel" onSubmit={handleSubmit} noValidate={false}>
      {error ? (
        <p className="notice notice--error" role="alert">
          {error}
        </p>
      ) : (
        <p className="notice notice--info">
          Your tutor will message the WhatsApp number you enter below. Please
          check it carefully - that is where everything happens.
        </p>
      )}

      <div className="field">
        <label htmlFor="student_name">Your name</label>
        <input
          id="student_name"
          name="student_name"
          required
          maxLength={200}
          autoComplete="name"
          placeholder="Aarav Sharma"
          disabled={busy}
        />
      </div>

      <div className="row">
        <div className="field">
          <label htmlFor="whatsapp_number">WhatsApp number</label>
          <input
            id="whatsapp_number"
            name="whatsapp_number"
            required
            inputMode="tel"
            autoComplete="tel"
            pattern="[0-9+\s\-()]{10,20}"
            placeholder="9999000001"
            disabled={busy}
          />
          <span className="hint">This is where your tutor will run.</span>
        </div>

        <div className="field">
          <label htmlFor="contact_phone">Contact number (optional)</label>
          <input
            id="contact_phone"
            name="contact_phone"
            inputMode="tel"
            placeholder="Parent&apos;s number, if different"
            disabled={busy}
          />
          <span className="hint">Only if it differs from the above.</span>
        </div>
      </div>

      <div className="field">
        <label htmlFor="tutor_name">What should your tutor be called?</label>
        <input
          id="tutor_name"
          name="tutor_name"
          required
          maxLength={200}
          placeholder="Anita Ma'am"
          disabled={busy}
        />
        {/* Stated on the form itself, not buried in terms. A student should
            never be left thinking a real person is reading their messages. */}
        <span className="hint">
          Your favourite teacher&apos;s name works well. Your tutor answers to
          it - it will never claim to actually be them.
        </span>
      </div>

      <div className="row">
        <div className="field">
          <label htmlFor="subject">Main subject</label>
          <input
            id="subject"
            name="subject"
            required
            maxLength={120}
            list="subjects"
            placeholder="Physics"
            disabled={busy}
          />
          <datalist id="subjects">
            <option value="Mathematics" />
            <option value="Physics" />
            <option value="Chemistry" />
            <option value="Biology" />
            <option value="English" />
            <option value="Computer Science" />
            <option value="Accountancy" />
            <option value="Economics" />
            <option value="All subjects" />
          </datalist>
        </div>

        <div className="field">
          <label htmlFor="location">City (optional)</label>
          <input
            id="location"
            name="location"
            maxLength={200}
            placeholder="Jaipur"
            disabled={busy}
          />
        </div>
      </div>

      <button className="btn btn--lg btn--block" type="submit" disabled={busy || !sdkReady}>
        {busy ? (
          <>
            <span className="spinner" aria-hidden="true" />
            {status === "redirecting" ? "Opening payment..." : "Setting up..."}
          </>
        ) : (
          "Pay ₹100 and activate"
        )}
      </button>

      <p className="muted center" style={{ marginTop: "1rem", fontSize: "0.86rem" }}>
        Secure payment by Cashfree. We never see your card details.
      </p>
    </form>
  );
}
