import type { Metadata, Viewport } from "next";

import "./globals.css";

const SITE = process.env.NEXT_PUBLIC_SITE_URL ?? "https://nxtutortwin.nxtutors.com";

/**
 * Metadata matters more here than on most pages: this link gets pasted into
 * WhatsApp, and the preview card *is* the advert for most of the people who
 * will ever see it.
 */
export const metadata: Metadata = {
  metadataBase: new URL(SITE),
  title: {
    default: "TutorTwin - your WhatsApp study buddy",
    template: "%s | TutorTwin",
  },
  description:
    "A tutor that lives in WhatsApp. Send a photo of your homework, a PDF, or a voice note - get worked solutions, hints and practice. Rs 100 a month.",
  openGraph: {
    type: "website",
    siteName: "TutorTwin",
    title: "TutorTwin - your WhatsApp study buddy",
    description:
      "Send a photo of any question. Get a step-by-step answer on WhatsApp. Rs 100 a month.",
    url: SITE,
    images: ["/logo.png"],
  },
  icons: { icon: "/logo.png", shortcut: "/logo.png", apple: "/logo.png" },
  twitter: {
    card: "summary_large_image",
    title: "TutorTwin - your WhatsApp study buddy",
    description: "Homework help on WhatsApp. Photos, PDFs and voice notes. Rs 100 a month.",
    images: ["/logo.png"],
  },
  robots: { index: true, follow: true },
};

export const viewport: Viewport = {
  themeColor: "#4f46e5",
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en-IN">
      <body>
        <header className="masthead">
          <div className="shell masthead__inner">
            <a className="wordmark" href="/">
              <span className="logo-mark wordmark__dot" aria-hidden="true">
                <img src="/logo.png" alt="" />
              </span>
              TutorTwin
            </a>
            <a className="btn" href="/payment">
              Get started
            </a>
          </div>
        </header>

        <main>{children}</main>

        <footer className="footer">
          <div className="shell footer__cols">
            <div>
              <a className="footer__brand" href="https://www.nxtutors.com/" target="_blank" rel="noopener">
                <span className="logo-mark logo-mark--lg" aria-hidden="true">
                  <img src="/logo.png" alt="" />
                </span>
                NXTutors▲
              </a>
              <strong style={{ color: "#fff" }}>TutorTwin</strong> by NX Tutors
              <br />
              Your day-to-day study buddy on WhatsApp.
              <p style={{ marginTop: "1rem" }}>
                <a className="btn" href="/payment">
                  Get subscription
                </a>
              </p>
            </div>

            {/* A real, verifiable postal address, phone and email. Cashfree
                requires reachable merchant contact details before a live
                account is approved, and a payment page without them reads as a
                scam to exactly the parents who are being asked to pay. */}
            <address className="footer__contact">
              <h3 className="footer__heading">Contact us</h3>

              <p className="footer__line">
                <span aria-hidden="true">📍</span>
                <span>
                  BLK-2/49, NXTutors Edtech Pvt Ltd,
                  <br />
                  M3M Cosmopolitan, off Golf Course Extension Road,
                  <br />
                  Sector 66, Gurugram, Haryana 122101
                </span>
              </p>

              <p className="footer__line">
                <span aria-hidden="true">📞</span>
                <a href="tel:+917836034313">+91 78360 34313</a>
              </p>

              <p className="footer__line">
                <span aria-hidden="true">✉️</span>
                <a href="mailto:support@nxtutors.com">support@nxtutors.com</a>
              </p>

              <p className="footer__line">
                <span aria-hidden="true">🌐</span>
                <a href="https://www.nxtutors.com/" target="_blank" rel="noopener">
                  www.nxtutors.com
                </a>
              </p>
            </address>
          </div>

          <div className="shell footer__fine">
            {/* Said plainly, because a student should never be misled about who
                is answering. The persona is named after their teacher; it does
                not claim to be them. */}
            <p>
              TutorTwin is an AI study assistant. It is not a human teacher, and
              it can make mistakes - always check important answers.
            </p>
            <p style={{ marginTop: "0.6rem" }}>
              © {new Date().getFullYear()} NXTutors Edtech Pvt Ltd. All rights
              reserved.
            </p>
          </div>
        </footer>
      </body>
    </html>
  );
}
