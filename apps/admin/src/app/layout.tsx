import type { Metadata } from "next";
import type { ReactNode } from "react";

import "./globals.css";

export const metadata: Metadata = {
  title: "TutorTwin Control Plane",
  description: "Operational control plane for the TutorTwin education agent.",
  // An admin tool must never be indexed, and must never leak a URL through a
  // referrer to whatever an operator clicks next.
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
