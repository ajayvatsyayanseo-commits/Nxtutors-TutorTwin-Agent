/**
 * Display helpers. Pure functions, unit-tested, no React.
 *
 * Money is the one worth reading closely. The API stores and reports **micros**
 * — millionths of a unit — as integers, because a float cannot hold a fraction
 * of a cent exactly and a cost report that disagrees with the invoice is worse
 * than no report. Conversion to a human string happens here, once, at the edge.
 */

export function formatMicros(micros: number, currency = "USD"): string {
  const units = micros / 1_000_000;
  // Sub-cent totals are the normal case on a cheap tier; rounding them to two
  // decimals would display "$0.00" beside a real spend.
  const fractionDigits = units !== 0 && Math.abs(units) < 0.01 ? 6 : 2;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }).format(units);
}

export function formatNumber(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("en-GB", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "UTC",
  }).format(date);
}

export function formatRelative(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";

  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ["year", 31_536_000],
    ["month", 2_592_000],
    ["day", 86_400],
    ["hour", 3_600],
    ["minute", 60],
  ];
  const formatter = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  for (const [unit, size] of units) {
    if (Math.abs(seconds) >= size) {
      return formatter.format(Math.round(seconds / size), unit);
    }
  }
  return formatter.format(seconds, "second");
}

/**
 * Accuracy is `null`, never `0`, below the evidence threshold.
 *
 * The learning engine refuses to report a percentage from three attempts, and
 * the UI must not undo that by rendering `0%` — which reads as "always wrong"
 * rather than "not enough data".
 */
export const MIN_ATTEMPTS_FOR_ACCURACY = 5;

export function formatAccuracy(attempts: number, correct: number): string {
  if (attempts < MIN_ATTEMPTS_FOR_ACCURACY) return "not enough data";
  return `${Math.round((100 * correct) / attempts)}%`;
}

export function truncate(text: string, max = 120): string {
  return text.length <= max ? text : `${text.slice(0, max - 1)}…`;
}

/** A short, stable label for a UUID in a dense table. */
export function shortId(value: string): string {
  return value.length > 8 ? value.slice(0, 8) : value;
}

export function titleCase(value: string): string {
  return value
    .toLowerCase()
    .split(/[\s_]+/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

/**
 * Paise to rupees.
 *
 * Integer paise everywhere on the wire, divided only here. Money that travels
 * as a float eventually pays somebody 99.99999 rupees, and the conversation
 * that follows is not one anybody wants to have with a customer.
 */
export function formatPaise(paise: number, currency = "INR"): string {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency,
    maximumFractionDigits: 2,
  }).format(paise / 100);
}
