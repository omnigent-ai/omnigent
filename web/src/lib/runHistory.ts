import {
  RUN_OUTCOMES,
  type ScheduledTaskRunOutcome,
  type ScheduledTaskRunStatsDay,
} from "@/lib/scheduledTasksApi";

/** The ranges an automation's run history chart offers, in days ending today. */
export const RUN_HISTORY_RANGES = [
  { days: 7, label: "Last 7 days" },
  { days: 28, label: "Last 4 weeks" },
  { days: 90, label: "Last 3 months" },
] as const;

export const DEFAULT_RUN_HISTORY_DAYS = 28;

/** What each outcome is called in the chart's legend, tooltip and table. */
export const RUN_OUTCOME_LABELS: Record<ScheduledTaskRunOutcome, string> = {
  succeeded: "Succeeded",
  failed: "Failed",
  skipped: "Skipped",
  inProgress: "In progress",
};

/** An automation's runs over a range of days, added up. */
export interface RunHistorySummary {
  counts: Record<ScheduledTaskRunOutcome, number>;
  total: number;
  /**
   * The share of finished runs that succeeded, as a whole percentage; `null`
   * while no run has succeeded or failed. Skipped runs never ran, so they
   * don't count for or against it.
   */
  successRate: number | null;
}

export function summarizeRunHistory(days: readonly ScheduledTaskRunStatsDay[]): RunHistorySummary {
  const counts = { succeeded: 0, failed: 0, skipped: 0, inProgress: 0 };
  for (const day of days) {
    for (const outcome of RUN_OUTCOMES) counts[outcome] += day[outcome];
  }
  const finished = counts.succeeded + counts.failed;
  let successRate: number | null = null;
  if (finished > 0) {
    successRate = Math.round((counts.succeeded / finished) * 100);
    // Rounding must not hide the one failure in a thousand runs, or the one success.
    if (counts.failed > 0) successRate = Math.min(successRate, 99);
    if (counts.succeeded > 0) successRate = Math.max(successRate, 1);
  }
  return {
    counts,
    total: RUN_OUTCOMES.reduce((sum, outcome) => sum + counts[outcome], 0),
    successRate,
  };
}

/** How many runs a day had, whatever their outcome. */
export function runHistoryDayTotal(day: ScheduledTaskRunStatsDay): number {
  return RUN_OUTCOMES.reduce((sum, outcome) => sum + day[outcome], 0);
}

/**
 * A stats day such as `2026-11-04` as a short label, e.g. `Nov 4`, or
 * `Wed, Nov 4` with its weekday. The day is a calendar date in the stats'
 * timezone, so it is formatted as that date and never shifted to the viewer's.
 */
export function formatRunHistoryDay(isoDate: string, { weekday = false } = {}): string {
  const [year, month, day] = isoDate.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day)).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
    ...(weekday ? { weekday: "short" } : {}),
  });
}

/** `1 run`, `12 runs` or `1,204 runs`. */
export function formatRunCount(count: number): string {
  return `${count.toLocaleString()} ${count === 1 ? "run" : "runs"}`;
}
