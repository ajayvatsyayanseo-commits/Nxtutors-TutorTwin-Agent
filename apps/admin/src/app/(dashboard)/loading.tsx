import { LoadingRows } from "@/components/ui";

/**
 * Streaming placeholder for every dashboard page.
 *
 * Next renders this while a server component awaits the API. Without it an
 * operator sees the previous page frozen and cannot tell a slow query from a
 * dead one.
 */
export default function Loading() {
  return (
    <>
      <div className="skeleton" style={{ width: 220, height: 24, marginBottom: 8 }} />
      <div className="skeleton" style={{ width: 380, height: 14, marginBottom: 24 }} />
      <LoadingRows rows={6} />
    </>
  );
}
