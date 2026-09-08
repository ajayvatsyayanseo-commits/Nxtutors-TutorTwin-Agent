import Link from "next/link";

/**
 * What an operator sees when the API refuses a read.
 *
 * Rendered under a real HTTP 403, not a 200 with an error panel: the refusal
 * came from the server and the response says so, which is what makes "hiding
 * the link is a courtesy, not a control" checkable rather than a claim.
 *
 * It does not say which role would be allowed. Telling someone exactly which
 * privilege to acquire is help for the wrong reader.
 */
export default function Forbidden() {
  return (
    <main id="main" style={{ padding: "48px 28px", maxWidth: "60ch" }}>
      <h2>Not permitted</h2>
      <p className="subtitle">
        Your role does not include this section. The TutorTwin API refused the request; this
        page is only reporting it.
      </p>
      <p>
        If you need it, ask a super administrator to review your role. Every role change is
        recorded in the audit log.
      </p>
      <p>
        <Link href="/">← Back to the dashboard</Link>
      </p>
    </main>
  );
}
