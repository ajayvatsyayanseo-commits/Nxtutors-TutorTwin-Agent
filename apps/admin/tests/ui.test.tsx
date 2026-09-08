import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  Badge,
  BoolBadge,
  DataTable,
  Empty,
  HighRiskFields,
  MoneyStat,
  Pagination,
  Segmented,
  ShareBar,
  Stat,
} from "@/components/ui";

describe("Stat", () => {
  it("formats numeric values and shows the hint", () => {
    render(<Stat label="Requests" value={1234} hint="last 7 days" />);
    expect(screen.getByText("1,234")).toBeInTheDocument();
    expect(screen.getByText("last 7 days")).toBeInTheDocument();
  });

  it("passes a pre-formatted string through untouched", () => {
    render(<Stat label="Created" value="4 Mar 2026, 09:30" />);
    expect(screen.getByText("4 Mar 2026, 09:30")).toBeInTheDocument();
  });
});

describe("MoneyStat", () => {
  it("renders micros as currency", () => {
    render(<MoneyStat label="Spend" micros={2_500_000} />);
    expect(screen.getByText("$2.50")).toBeInTheDocument();
  });
});

describe("Empty", () => {
  it("distinguishes 'no results' from 'nothing exists'", () => {
    render(<Empty title="No students match those filters" hint="Clear the filters." />);
    expect(screen.getByText("No students match those filters")).toBeInTheDocument();
    expect(screen.getByText("Clear the filters.")).toBeInTheDocument();
  });
});

describe("Badge", () => {
  it("shows an em dash for a missing value rather than an empty pill", () => {
    render(<Badge value={null} />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("tones a known state", () => {
    const { container } = render(<Badge value="FAILED" />);
    expect(container.querySelector(".badge-bad")).not.toBeNull();
  });

  it("renders an unknown state without a tone rather than dropping it", () => {
    const { container } = render(<Badge value="SOMETHING_NEW" />);
    expect(screen.getByText("SOMETHING_NEW")).toBeInTheDocument();
    expect(container.querySelector(".badge-bad")).toBeNull();
  });
});

describe("BoolBadge", () => {
  it("labels both states", () => {
    const { rerender } = render(<BoolBadge value on="enabled" off="disabled" />);
    expect(screen.getByText("enabled")).toBeInTheDocument();
    rerender(<BoolBadge value={false} on="enabled" off="disabled" />);
    expect(screen.getByText("disabled")).toBeInTheDocument();
  });
});

describe("DataTable", () => {
  it("renders accessible column headers", () => {
    render(
      <DataTable
        caption="Students"
        columns={[
          { key: "a", label: "Identity" },
          { key: "b", label: "Calls", numeric: true },
        ]}
      >
        <tr>
          <td>+919999000001</td>
          <td className="numeric">12</td>
        </tr>
      </DataTable>,
    );

    expect(screen.getByRole("columnheader", { name: "Identity" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Calls" })).toBeInTheDocument();
    const row = screen.getAllByRole("row")[1];
    expect(within(row!).getByText("+919999000001")).toBeInTheDocument();
  });
});

describe("Pagination", () => {
  it("reports the visible range and disables the edges", () => {
    render(
      <Pagination page={1} pageSize={25} total={60} basePath="/students" query={{ q: "abc" }} />,
    );

    expect(screen.getByText("1–25 of 60")).toBeInTheDocument();
    expect(screen.getByText("Page 1 of 3")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Previous" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(screen.getByRole("link", { name: "Next" })).toHaveAttribute(
      "aria-disabled",
      "false",
    );
  });

  it("carries the active filters into the page links", () => {
    // Paging that drops the filter silently shows an operator a different result
    // set than the one they are reading.
    render(
      <Pagination
        page={2}
        pageSize={25}
        total={60}
        basePath="/students"
        query={{ q: "abc", status: "ACTIVE" }}
      />,
    );

    const next = screen.getByRole("link", { name: "Next" });
    expect(next).toHaveAttribute("href", "/students?q=abc&status=ACTIVE&page=3");
  });

  it("shows 0 of 0 for an empty result rather than 1–25 of 0", () => {
    render(<Pagination page={1} pageSize={25} total={0} basePath="/jobs" query={{}} />);
    expect(screen.getByText("0–0 of 0")).toBeInTheDocument();
  });
});

describe("HighRiskFields", () => {
  it("requires a reason of at least eight characters and offers an explicit tick", () => {
    render(
      <form>
        <HighRiskFields actionLabel="stops this capability" />
      </form>,
    );

    const reason = screen.getByLabelText(/Reason/);
    expect(reason).toBeRequired();
    expect(reason).toHaveAttribute("minLength", "8");

    // The confirmation is deliberately not `required`: the API refuses the
    // mutation without it, and a native validation bubble inside a collapsed
    // panel would hide that answer rather than deliver it.
    const confirm = screen.getByRole("checkbox");
    expect(confirm).not.toBeRequired();
    expect(confirm).toHaveAttribute("name", "confirm");
    expect(screen.getByText(/stops this capability/)).toBeInTheDocument();

    // The hint tells the operator the reason is *kept*, not merely required —
    // and the field points at it, so a screen reader announces it too.
    const hintId = reason.getAttribute("aria-describedby");
    expect(hintId).toBeTruthy();
    expect(document.getElementById(hintId!)?.textContent).toMatch(
      /stored in the audit log against your account/i,
    );
  });

  it("gives every instance its own ids, so ten switches are ten separate forms", () => {
    // With a literal id="reason", every label on a page of kill switches points
    // at the first field: the form looks right and edits the wrong row.
    render(
      <form>
        <HighRiskFields actionLabel="disables pdf_processing" />
        <HighRiskFields actionLabel="disables rag_retrieval" />
      </form>,
    );

    const [first, second] = screen.getAllByLabelText(/Reason/);
    expect(first).toBeDefined();
    expect(second).toBeDefined();
    expect(first!.id).not.toBe(second!.id);
    expect(screen.getAllByRole("checkbox")).toHaveLength(2);
  });
});

describe("ShareBar", () => {
  it("draws the proportion as a width", () => {
    const { container } = render(<ShareBar share={0.25} />);
    expect(container.querySelector<HTMLElement>(".bar-fill")?.style.width).toBe("25%");
  });

  it("clamps out-of-range and non-finite shares instead of drawing off the card", () => {
    // A denominator of zero produces NaN and a rounding artefact produces 1.004.
    // Both are arithmetic, not operator error, so neither should reach the DOM.
    const { container, rerender } = render(<ShareBar share={1.4} />);
    expect(container.querySelector<HTMLElement>(".bar-fill")?.style.width).toBe("100%");
    rerender(<ShareBar share={Number.NaN} />);
    expect(container.querySelector<HTMLElement>(".bar-fill")?.style.width).toBe("0%");
    rerender(<ShareBar share={-3} />);
    expect(container.querySelector<HTMLElement>(".bar-fill")?.style.width).toBe("0%");
  });

  it("is hidden from assistive technology, because the number is in the next cell", () => {
    const { container } = render(<ShareBar share={0.5} />);
    expect(container.querySelector(".bar-track")).toHaveAttribute("aria-hidden", "true");
  });
});

describe("Stat", () => {
  it("carries a tone as data, so one rule styles the rail", () => {
    const { container } = render(<Stat label="Failed" value={3} tone="bad" />);
    expect(container.querySelector(".card")).toHaveAttribute("data-tone", "bad");
  });

  it("omits the meter unless a share was given", () => {
    const { container } = render(<Stat label="Requests" value={3} />);
    expect(container.querySelector(".bar-track")).toBeNull();
  });
});

describe("Segmented", () => {
  it("marks the current option and links every other one", () => {
    render(
      <Segmented
        label="Time window"
        current={7}
        options={[
          { value: 1, label: "1d" },
          { value: 7, label: "7d" },
        ]}
        href={(value) => `/?window=${value}`}
      />,
    );

    expect(screen.getByRole("link", { name: "7d" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "1d" })).toHaveAttribute("href", "/?window=1");
  });
});
