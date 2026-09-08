import type { Metadata } from "next";

import { SignupForm } from "@/components/signup-form";

export const metadata: Metadata = {
  title: "Get your subscription",
  description: "Rs 100 for 30 days. Activated on WhatsApp within seconds of payment.",
  robots: { index: false, follow: true },
};

export default function PaymentPage() {
  return (
    <section className="section">
      <div className="shell">
        <div className="section__head center" style={{ marginInline: "auto" }}>
          <h2>Set up your tutor</h2>
          <p>
            Four things, then payment. Your tutor says hello on WhatsApp the
            moment it clears.
          </p>
        </div>

        <SignupForm />
      </div>
    </section>
  );
}
