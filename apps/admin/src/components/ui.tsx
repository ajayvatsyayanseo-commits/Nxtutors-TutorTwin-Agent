import Link from "next/link";
import type { ReactNode } from "react";

import { formatMicros, formatNumber } from "@/lib/format";

/**
 * Re-exported so every page keeps importing its vocabulary from one place.
 * It lives in its own module because it needs `useId`, and therefore "use client".
 */
export { HighRiskFields } from "./high-risk-fields";

/**
 * The shared vocabulary of the control plane: stat, table, states, pagination,
 * badge, confirmation.
 *
 * All server components. Nothing here needs interactivity, so nothing here ships
 * JavaScript to the browser — which is also why the session token can never leak
 * into a client bundle.
 */

/** A colour rail on a stat card. Absent by default: not every number has a mood. */
export type Tone = "ok" | "warn" | "bad" | "accent";

export function Stat({
  label,
  value,
  hint,
  tone,
  share,
}: {
  label: string;
  value: string | number;
  hint?: string;
  tone?: Tone;
  /** 0–1. Draws a proportion bar under the value. */
  share?: number;
}) {
  return (
    <div className="card" data-tone={tone}>
      <div className="label">{label}</div>
      <div className="value">{typeof value === "number" ? formatNumber(value) : value}</div>
      {hint ? <div className="hint">{hint}</div> : null}
      {share === undefined ? null : (
        <div className="meter">
          <ShareBar share={share} tone={tone} />
        </div>
      )}
    </div>
  );
}

export function MoneyStat({
  label,
  micros,
  hint,
  tone,
}: {
  label: string;
  micros: number;
  hint?: string;
  tone?: Tone;
}) {
  return <Stat label={label} value={formatMicros(micros)} hint={hint} tone={tone} />;
}

const TONE_COLOUR: Record<Tone, string> = {
  ok: "var(--success)",
  warn: "var(--warning)",
  bad: "var(--danger)",
  accent: "var(--accent)",
};

/**
 * A proportion, drawn as a width.
 *
 * No chart library: every comparison in this control plane is "this row against
 * the total", which is one div inside another. Shipping a charting runtime to
 * the browser to draw a rectangle would also mean relaxing the CSP that keeps
 * third-party script off these pages.
 *
 * `aria-hidden` because the number it illustrates is always in the adjacent
 * cell — a screen reader should hear "42%", not "progressbar, 42".
 */
export function ShareBar({ share, tone }: { share: number; tone?: Tone }) {
  const clamped = Math.max(0, Math.min(1, Number.isFinite(share) ? share : 0));
  return (
    <div className="bar-track" aria-hidden="true">
      <div
        className="bar-fill"
        style={{
          width: `${clamped * 100}%`,
          background: tone ? TONE_COLOUR[tone] : undefined,
        }}
      />
    </div>
  );
}

/**
 * A window/filter switch built from links.
 *
 * Each option is a real URL, so it is shareable, bookmarkable, back-buttonable
 * and works with JavaScript disabled. A client-side toggle would be none of
 * those and would ship a bundle to do less.
 */
export function Segmented({
  label,
  options,
  current,
  href,
}: {
  label: string;
  options: { value: string | number; label: string }[];
  current: string | number;
  href: (value: string | number) => string;
}) {
  return (
    <nav aria-label={label} className="segmented">
      {options.map((option) => (
        <Link
          key={option.value}
          href={href(option.value)}
          aria-current={option.value === current ? "page" : undefined}
        >
          {option.label}
        </Link>
      ))}
    </nav>
  );
}

/**
 * Empty is a first-class state, not a blank table.
 *
 * "No results" and "nothing exists yet" are different situations, and an
 * operator who cannot tell them apart will assume the tool is broken.
 */
export function Empty({
  title,
  hint,
}: {
  title: string;
  hint?: string;
}) {
  return (
    <div className="empty">
      <p style={{ margin: 0, fontWeight: 600 }}>{title}</p>
      {hint ? <p style={{ margin: "6px 0 0" }}>{hint}</p> : null}
    </div>
  );
}

/** Shown by Next while a server component streams. */
export function LoadingRows({ rows = 5 }: { rows?: number }) {
  return (
    <div className="table-wrap" aria-busy="true" aria-live="polite">
      <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 10 }}>
        <span className="skeleton" style={{ width: "40%" }} />
        {Array.from({ length: rows }, (_, index) => (
          <span key={index} className="skeleton" style={{ width: `${90 - index * 6}%` }} />
        ))}
        <span className="skeleton" style={{ width: "25%" }} />
      </div>
      <span className="visually-hidden">Loading</span>
    </div>
  );
}

export function ErrorNotice({
  title,
  detail,
}: {
  title: string;
  detail?: string;
}) {
  return (
    <div className="alert alert-error" role="alert">
      <strong>{title}</strong>
      {detail ? <div style={{ marginTop: 4 }}>{detail}</div> : null}
    </div>
  );
}

export function Notice({
  kind,
  children,
}: {
  kind: "error" | "warning" | "success";
  children: ReactNode;
}) {
  return (
    <div className={`alert alert-${kind}`} role={kind === "error" ? "alert" : "status"}>
      {children}
    </div>
  );
}

const BADGE_TONE: Record<string, string> = {
  ACTIVE: "badge-ok",
  COMPLETED: "badge-ok",
  SUCCEEDED: "badge-ok",
  READY: "badge-ok",
  GRADED: "badge-ok",
  OPEN: "badge-ok",
  PENDING: "badge-warn",
  RUNNING: "badge-warn",
  IN_PROGRESS: "badge-warn",
  SUBMITTED: "badge-warn",
  ASSIGNED: "badge-warn",
  DISABLED: "badge-bad",
  FAILED: "badge-bad",
  FAILED_PERMANENT: "badge-bad",
  REJECTED: "badge-bad",
  CANCELLED: "badge-bad",
  ERROR: "badge-bad",
};

export function Badge({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="badge">—</span>;
  return <span className={`badge ${BADGE_TONE[value] ?? ""}`}>{value}</span>;
}

export function BoolBadge({ value, on = "ON", off = "OFF" }: {
  value: boolean;
  on?: string;
  off?: string;
}) {
  return <span className={`badge ${value ? "badge-ok" : "badge-bad"}`}>{value ? on : off}</span>;
}

/**
 * Server-side pagination controls.
 *
 * The links carry the whole current query, so a filtered search survives paging.
 * Client-side pagination over a full table download was never an option: the
 * tables here are unbounded.
 */
export function Pagination({
  page,
  pageSize,
  total,
  basePath,
  query,
}: {
  page: number;
  pageSize: number;
  total: number;
  basePath: string;
  query: Record<string, string | undefined>;
}) {
  const lastPage = Math.max(1, Math.ceil(total / pageSize));
  const build = (target: number) => {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) {
      if (value) params.set(key, value);
    }
    params.set("page", String(target));
    return `${basePath}?${params.toString()}`;
  };

  const first = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const last = Math.min(page * pageSize, total);

  return (
    <nav className="pagination" aria-label="Pagination">
      <span>
        {first}–{last} of {formatNumber(total)}
      </span>
      <Link
        href={build(page - 1)}
        className={page <= 1 ? "disabled" : ""}
        aria-disabled={page <= 1}
        rel="prev"
      >
        Previous
      </Link>
      <Link
        href={build(page + 1)}
        className={page >= lastPage ? "disabled" : ""}
        aria-disabled={page >= lastPage}
        rel="next"
      >
        Next
      </Link>
      <span>
        Page {page} of {lastPage}
      </span>
    </nav>
  );
}

export function DataTable({
  columns,
  children,
  caption,
}: {
  columns: { key: string; label: string; numeric?: boolean; wrap?: boolean }[];
  children: ReactNode;
  caption?: string;
}) {
  return (
    <div className="table-wrap">
      <table>
        {caption ? <caption className="visually-hidden">{caption}</caption> : null}
        <thead>
          <tr>
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                className={[column.numeric ? "numeric" : "", column.wrap ? "wrap" : ""]
                  .filter(Boolean)
                  .join(" ")}
              >
                {column.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        <h2>{title}</h2>
        {subtitle ? <p className="subtitle">{subtitle}</p> : null}
      </div>
      {actions ? <div className="row-actions">{actions}</div> : null}
    </header>
  );
}
