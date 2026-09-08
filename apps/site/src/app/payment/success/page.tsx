import type { Metadata } from "next";

import { ActivationWatcher } from "@/components/activation-watcher";

export const metadata: Metadata = {
  title: "Payment received",
  robots: { index: false, follow: false },
};

/**
 * Where Cashfree returns the student after checkout.
 *
 * Landing here proves nothing. The browser is returned to this URL whether the
 * payment succeeded, failed, or the student pressed back - and the URL can be
 * typed by hand. So this page **asks the server** what happened and reports
 * that, rather than congratulating anybody for having arrived.
 */
export default async function SuccessPage({
  searchParams,
}: {
  searchParams: Promise<{ order_id?: string }>;
}) {
  const { order_id: orderId } = await searchParams;

  return (
    <section className="section">
      <div className="shell">
        <div className="panel center">
          {orderId ? (
            <ActivationWatcher orderId={orderId} />
          ) : (
            <>
              <h2>No order to check</h2>
              <p className="muted" style={{ marginTop: "1rem" }}>
                This page needs an order reference. If you have just paid, your
                tutor will message you on WhatsApp shortly regardless - the
                subscription is activated by our payment provider, not by this
                page.
              </p>
              <a className="btn" href="/payment" style={{ marginTop: "1.75rem" }}>
                Back to signup
              </a>
            </>
          )}
        </div>
      </div>
    </section>
  );
}
