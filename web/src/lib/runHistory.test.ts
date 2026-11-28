import { describe, expect, it, vi } from "vitest";
import {
  formatRunCount,
  formatRunHistoryDay,
  runHistoryDayTotal,
  summarizeRunHistory,
} from "./runHistory";
import type { ScheduledTaskRunStatsDay } from "./scheduledTasksApi";

function day(
  date: string,
  counts: Partial<ScheduledTaskRunStatsDay> = {},
): ScheduledTaskRunStatsDay {
  return { date, succeeded: 0, failed: 0, skipped: 0, inProgress: 0, ...counts };
}

describe("summarizeRunHistory", () => {
  it("adds up every day's runs by outcome", () => {
    const summary = summarizeRunHistory([
      day("2026-11-03", { succeeded: 2, skipped: 1 }),
      day("2026-11-04"),
      day("2026-11-05", { succeeded: 1, failed: 1, inProgress: 1 }),
    ]);

    expect(summary.counts).toEqual({ succeeded: 3, failed: 1, skipped: 1, inProgress: 1 });
    expect(summary.total).toBe(6);
    // Three of the four finished runs succeeded; skipped and unfinished runs don't count.
    expect(summary.successRate).toBe(75);
  });

  it("has no success rate until a run succeeds or fails", () => {
    expect(summarizeRunHistory([]).successRate).toBeNull();
    expect(
      summarizeRunHistory([day("2026-11-04", { skipped: 3, inProgress: 1 })]).successRate,
    ).toBeNull();
    expect(summarizeRunHistory([day("2026-11-04", { failed: 3 })]).successRate).toBe(0);
    expect(summarizeRunHistory([day("2026-11-04", { succeeded: 3 })]).successRate).toBe(100);
  });

  it("never rounds a failure or a success away", () => {
    expect(
      summarizeRunHistory([day("2026-11-04", { succeeded: 999, failed: 1 })]).successRate,
    ).toBe(99);
    expect(
      summarizeRunHistory([day("2026-11-04", { succeeded: 1, failed: 999 })]).successRate,
    ).toBe(1);
  });
});

it("totals a day's runs across outcomes", () => {
  expect(runHistoryDayTotal(day("2026-11-04", { succeeded: 2, failed: 1, inProgress: 1 }))).toBe(4);
});

it("formats a stats day as that calendar date in any viewer timezone", () => {
  expect(formatRunHistoryDay("2026-11-04")).toBe("Nov 4");
  expect(formatRunHistoryDay("2026-01-01", { weekday: true })).toBe("Thu, Jan 1");
  // West of UTC, the date's midnight in UTC is still the day before.
  vi.stubEnv("TZ", "America/Los_Angeles");
  try {
    expect(new Date(Date.UTC(2026, 10, 4)).getDate()).toBe(3);
    expect(formatRunHistoryDay("2026-11-04")).toBe("Nov 4");
    expect(formatRunHistoryDay("2026-01-01", { weekday: true })).toBe("Thu, Jan 1");
  } finally {
    vi.unstubAllEnvs();
  }
});

it("counts runs in words", () => {
  expect(formatRunCount(1)).toBe("1 run");
  expect(formatRunCount(1204)).toBe("1,204 runs");
});
