import { describe, expect, it } from "vitest";

import {
  MIN_ATTEMPTS_FOR_ACCURACY,
  formatAccuracy,
  formatDateTime,
  formatMicros,
  formatNumber,
  shortId,
  titleCase,
  truncate,
} from "@/lib/format";

/**
 * These are display rules with correctness consequences, which is why they are
 * tested rather than eyeballed: a cost shown as $0.00 and a "0% accuracy" on
 * three attempts are both wrong in ways an operator would act on.
 */

describe("formatMicros", () => {
  it("renders whole units", () => {
    expect(formatMicros(1_000_000)).toBe("$1.00");
    expect(formatMicros(2_500_000)).toBe("$2.50");
  });

  it("does not round a real sub-cent spend down to zero", () => {
    // A cheap tier answers thousands of questions for a few cents. Two decimals
    // would display "$0.00" next to a genuine, growing bill.
    const rendered = formatMicros(1_500);
    expect(rendered).not.toBe("$0.00");
    expect(rendered).toBe("$0.001500");
  });

  it("renders exact zero plainly", () => {
    expect(formatMicros(0)).toBe("$0.00");
  });
});

describe("formatAccuracy", () => {
  it("refuses to report a percentage below the evidence threshold", () => {
    // The learning engine returns null here rather than 0.0; rendering "0%"
    // would undo that and read as "always wrong" instead of "not enough data".
    expect(formatAccuracy(3, 0)).toBe("not enough data");
    expect(formatAccuracy(MIN_ATTEMPTS_FOR_ACCURACY - 1, 4)).toBe("not enough data");
  });

  it("reports a percentage once there is enough evidence", () => {
    expect(formatAccuracy(10, 3)).toBe("30%");
    expect(formatAccuracy(5, 5)).toBe("100%");
  });
});

describe("formatDateTime", () => {
  it("renders UTC so two operators in different places read the same string", () => {
    expect(formatDateTime("2026-03-04T09:30:00Z")).toBe("4 Mar 2026, 09:30");
  });

  it("shows an em dash rather than 'Invalid Date'", () => {
    expect(formatDateTime(null)).toBe("—");
    expect(formatDateTime(undefined)).toBe("—");
    expect(formatDateTime("not a date")).toBe("—");
  });
});

describe("small helpers", () => {
  it("formats numbers with separators", () => {
    expect(formatNumber(1234567)).toBe("1,234,567");
  });

  it("truncates with an ellipsis only when needed", () => {
    expect(truncate("short", 10)).toBe("short");
    expect(truncate("abcdefghij", 5)).toBe("abcd…");
  });

  it("shortens ids without mangling short ones", () => {
    expect(shortId("abcdef1234567890")).toBe("abcdef12");
    expect(shortId("abc")).toBe("abc");
  });

  it("title-cases snake and space separated words", () => {
    expect(titleCase("FEATURE_KILL_SWITCH")).toBe("Feature Kill Switch");
    expect(titleCase("model route change")).toBe("Model Route Change");
  });
});
