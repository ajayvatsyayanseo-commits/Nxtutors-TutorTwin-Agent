import Link from "next/link";

export default function NotFound() {
  return (
    <div className="empty">
      <p style={{ margin: 0, fontWeight: 600 }}>Not found</p>
      <p>That record does not exist, or it was deleted.</p>
      <p>
        <Link href="/">Back to the dashboard</Link>
      </p>
    </div>
  );
}
