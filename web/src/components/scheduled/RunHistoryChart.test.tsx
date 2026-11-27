import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { cloneElement, type ReactElement } from "react";
import type * as Recharts from "recharts";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ScheduledTaskRunStats, ScheduledTaskRunStatsDay } from "@/lib/scheduledTasksApi";
import { RunHistoryChart } from "./RunHistoryChart";

// jsdom lays nothing out, so the chart gets the size its container would give it.
vi.mock("recharts", async (importOriginal) => ({
  ...(await importOriginal<typeof Recharts>()),
  ResponsiveContainer: ({
    children,
  }: {
    children: ReactElement<{ width: number; height: number }>;
  }) => cloneElement(children, { width: 600, height: 176 }),
}));

afterEach(cleanup);

function day(
  date: string,
  counts: Partial<ScheduledTaskRunStatsDay> = {},
): ScheduledTaskRunStatsDay {
  return { date, succeeded: 0, failed: 0, skipped: 0, inProgress: 0, ...counts };
}

const STATS: ScheduledTaskRunStats = {
  timezone: "UTC",
  days: [
    day("2026-11-02"),
    day("2026-11-03", { succeeded: 2, skipped: 1 }),
    day("2026-11-04", { succeeded: 1, failed: 1, inProgress: 1 }),
  ],
  truncated: false,
};

function renderChart(props: Partial<Parameters<typeof RunHistoryChart>[0]> = {}) {
  const onDaysChange = vi.fn();
  render(
    <RunHistoryChart
      stats={STATS}
      isError={false}
      isStale={false}
      days={28}
      onDaysChange={onDaysChange}
      {...props}
    />,
  );
  return onDaysChange;
}

/** The drawn parts of every column, in the order the chart draws its outcomes. */
function columnParts(): SVGElement[] {
  return Array.from(
    screen
      .getByTestId("run-history-chart")
      .querySelectorAll<SVGElement>(".recharts-bar-rectangle rect, .recharts-bar-rectangle path"),
  );
}

/** The tooltip's lines: the day, then each of its outcomes as "count outcome". */
function tooltipLines(): string[] {
  const tooltip = screen
    .getByTestId("run-history-chart")
    .querySelector(".recharts-tooltip-wrapper");
  return Array.from(tooltip?.querySelectorAll("p") ?? [], (line) => {
    const words = Array.from(line.querySelectorAll("span"), (span) => span.textContent);
    return words.filter(Boolean).join(" ") || (line.textContent ?? "");
  });
}

describe("RunHistoryChart", () => {
  it("sums up the range and names every outcome with its count", () => {
    renderChart();

    // Three of the four finished runs succeeded.
    expect(screen.getByTestId("run-history-summary")).toHaveTextContent(
      "6 runs · 75% success rate",
    );
    const legend = within(screen.getByTestId("run-history-legend"))
      .getAllByRole("listitem")
      .map((item) => item.textContent);
    expect(legend).toEqual(["Succeeded3", "Failed1", "Skipped1", "In progress1"]);
  });

  it("lists each day that had runs in a table for screen readers", () => {
    renderChart();

    const table = screen.getByRole("table", { name: "Runs per day, last 4 weeks" });
    const rows = within(table)
      .getAllByRole("row")
      .map((row) => Array.from(row.querySelectorAll("th, td"), (cell) => cell.textContent));
    expect(rows).toEqual([
      ["Day", "Succeeded", "Failed", "Skipped", "In progress"],
      ["Tue, Nov 3", "2", "0", "1", "0"],
      ["Wed, Nov 4", "1", "1", "0", "1"],
    ]);
  });

  it("stacks a day's outcomes into one column, and draws none for a day without runs", () => {
    renderChart();

    // 2 November had no runs. Each column's top part is the rounded one.
    const parts = columnParts();
    expect(parts.map((part) => [part.tagName, part.getAttribute("fill")])).toEqual([
      ["rect", "var(--chart-3)"],
      ["rect", "var(--chart-3)"],
      ["rect", "#c8324c"],
      ["path", "var(--chart-5)"],
      ["path", "var(--chart-1)"],
    ]);
    // 4 November: failed sits on succeeded, with a gap as well as a color between them.
    const [, succeeded, failed] = parts;
    const top = (part: SVGElement) => Number(part.getAttribute("y"));
    expect(failed.getAttribute("x")).toBe(succeeded.getAttribute("x"));
    expect(top(succeeded) - (top(failed) + Number(failed.getAttribute("height")))).toBeCloseTo(2);
  });

  it("lists a day's counts when it is hovered", async () => {
    renderChart();
    const plot = screen.getByTestId("run-history-chart").querySelector(".recharts-wrapper");
    const [tuesday] = columnParts();

    fireEvent.mouseMove(plot as Element, {
      clientX: Number(tuesday.getAttribute("x")) + Number(tuesday.getAttribute("width")) / 2,
      clientY: 80,
    });

    await waitFor(() => expect(tooltipLines()).toEqual(["Tue, Nov 3", "2 succeeded", "1 skipped"]));
    fireEvent.mouseLeave(plot as Element);
    await waitFor(() => expect(tooltipLines()).toEqual([]));
  });

  it("moves through the days' counts with the arrow keys", async () => {
    renderChart();
    const plot = screen.getByTestId("run-history-chart").querySelector("svg.recharts-surface");

    fireEvent.focus(plot as Element);
    await waitFor(() => expect(tooltipLines()).toEqual(["Mon, Nov 2", "No runs"]));
    fireEvent.keyDown(plot as Element, { key: "ArrowRight" });
    fireEvent.keyDown(plot as Element, { key: "ArrowRight" });
    await waitFor(() =>
      expect(tooltipLines()).toEqual(["Wed, Nov 4", "1 succeeded", "1 failed", "1 in progress"]),
    );
  });

  it("gives no success rate until a run has succeeded or failed", () => {
    renderChart({
      stats: { timezone: "UTC", days: [day("2026-11-04", { inProgress: 1 })], truncated: false },
    });

    expect(screen.getByTestId("run-history-summary")).toHaveTextContent(/^1 run$/);
  });

  it("says so, without a chart, when the range had no runs", () => {
    renderChart({
      stats: { timezone: "UTC", days: [day("2026-11-04")], truncated: false },
      days: 7,
    });

    expect(screen.getByTestId("run-history-summary")).toHaveTextContent(
      "No runs in the last 7 days.",
    );
    expect(screen.queryByTestId("run-history-legend")).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    cleanup();

    // The empty range was the previous one; the 4 weeks now chosen are still loading.
    renderChart({
      stats: { timezone: "UTC", days: [day("2026-11-04")], truncated: false },
      isStale: true,
    });
    expect(screen.getByTestId("run-history-summary")).toHaveTextContent("Loading run history…");
    expect(screen.getByTestId("run-history-chart")).toHaveAttribute("aria-busy", "true");
  });

  it("keeps the previous range's counts, dimmed, while the chosen range loads", () => {
    renderChart({ isStale: true, days: 90 });

    const summary = screen.getByTestId("run-history-summary");
    expect(summary).toHaveTextContent("6 runs · 75% success rate");
    expect(summary).toHaveClass("opacity-50");
    expect(screen.getByTestId("run-history-legend").parentElement).toHaveClass("opacity-50");
    expect(screen.getByTestId("run-history-chart")).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByTestId("run-history-refresh-error")).not.toBeInTheDocument();
  });

  it("says when only the most recent runs of the range are counted", () => {
    renderChart();
    expect(screen.queryByTestId("run-history-truncated")).not.toBeInTheDocument();
    cleanup();

    renderChart({ stats: { ...STATS, truncated: true } });
    expect(screen.getByTestId("run-history-truncated")).toHaveTextContent(
      "Only the most recent 6 runs in this range are counted.",
    );
    expect(screen.getByTestId("run-history-truncated")).not.toHaveClass("opacity-50");
    cleanup();

    // While another range loads, the note is as outdated as the counts it describes.
    renderChart({ stats: { ...STATS, truncated: true }, isStale: true });
    expect(screen.getByTestId("run-history-truncated")).toHaveClass("opacity-50");
  });

  it("says whether the counts are loading or failed to load", () => {
    renderChart({ stats: undefined });
    expect(screen.getByTestId("run-history-summary")).toHaveTextContent("Loading run history…");
    cleanup();

    renderChart({ stats: undefined, isError: true });
    expect(screen.getByTestId("run-history-summary")).toHaveTextContent(
      "Couldn’t load the run chart.",
    );
    expect(screen.getByTestId("run-history-chart")).toHaveAttribute("aria-busy", "false");
    cleanup();

    // A refresh that failed keeps the counts from before it, and says they may be old.
    renderChart({ isError: true });
    expect(screen.getByTestId("run-history-refresh-error")).toHaveTextContent(
      "Couldn’t refresh the run chart. These counts may be out of date.",
    );
    const summary = screen.getByTestId("run-history-summary");
    expect(summary).toHaveTextContent("6 runs · 75% success rate");
    expect(summary).toHaveClass("opacity-50");
    expect(screen.getByTestId("run-history-legend").parentElement).toHaveClass("opacity-50");
    cleanup();

    // An empty range says the same.
    renderChart({
      stats: { timezone: "UTC", days: [day("2026-11-04")], truncated: false },
      isError: true,
    });
    expect(screen.getByTestId("run-history-summary")).toHaveTextContent(
      "No runs in the last 4 weeks.",
    );
    expect(screen.getByTestId("run-history-refresh-error")).toBeInTheDocument();
    cleanup();

    renderChart();
    expect(screen.queryByTestId("run-history-refresh-error")).not.toBeInTheDocument();
    expect(screen.getByTestId("run-history-summary")).not.toHaveClass("opacity-50");
  });

  it("offers the ranges and reports the one picked", () => {
    const onDaysChange = renderChart();

    expect(screen.getByTestId("run-history-range")).toHaveTextContent("Last 4 weeks");
    fireEvent.click(screen.getByTestId("run-history-range"));
    expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual([
      "Last 7 days",
      "Last 4 weeks",
      "Last 3 months",
    ]);
    fireEvent.click(screen.getByRole("option", { name: "Last 3 months" }));
    expect(onDaysChange).toHaveBeenCalledWith(90);
  });
});
