// Build an RFC-5545 RRULE string from the manual create form's schedule model,
// and validate it against the form's constraints before submit.
//
// Top-level presets (the frequency dropdown): Hourly, Daily, Weekdays, Weekly,
// Custom. Monthly and Yearly are reachable ONLY under Custom (a sub-frequency
// selector with an interval + specifics). The server validates the final rule
// (`validate_rrule`), including a 1-hour-minimum-interval floor
// (MIN_INTERVAL_SECONDS=3600); this module enforces the same floor client-side
// (Custom Hourly INTERVAL ≥ 1) plus positive-integer intervals and non-empty
// multi-selects, so the form never submits a rule the server would reject.

/** The frequency dropdown's top-level options. */
export type SchedulePreset = "hourly" | "daily" | "weekdays" | "weekly" | "custom";

/** The frequency sub-selector shown under the "custom" preset. */
export type CustomFreq = "hourly" | "daily" | "weekly" | "monthly" | "yearly";

/** Weekday codes as used by RRULE `BYDAY` (MO..SU), in calendar order. */
export const WEEKDAY_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"] as const;
export type WeekdayCode = (typeof WEEKDAY_CODES)[number];

/** Months 1–12 for the Custom Yearly `BYMONTH`. */
export const MONTHS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12] as const;

/** The manual form's schedule inputs. Fields not relevant to the active
 * preset/custom-frequency are simply ignored by `buildRRule`. */
export interface ScheduleModel {
  preset: SchedulePreset;
  /** Hour of day 0–23 (ignored for hourly). */
  hour: number;
  /** Minute of hour 0–59. */
  minute: number;
  /** Selected weekdays for the `weekly` preset and Custom Weekly. */
  weekdays: WeekdayCode[];
  /** Sub-frequency when `preset === "custom"`. */
  customFreq: CustomFreq;
  /** Recurrence interval (every X units) for Custom; positive integer. */
  interval: number;
  /** Days-of-month (1–31) for Custom Monthly / Yearly (multi-select). */
  monthDays: number[];
  /** Month 1–12 for Custom Yearly. */
  month: number;
}

export const DEFAULT_SCHEDULE_MODEL: ScheduleModel = {
  preset: "daily",
  hour: 9,
  minute: 0,
  weekdays: ["MO"],
  customFreq: "daily",
  interval: 1,
  monthDays: [1],
  month: 1,
};

/**
 * Build the canonical RRULE string for a schedule model. Produces a plain
 * `RRULE:`-less rule body (e.g. `FREQ=WEEKLY;BYDAY=MO,TU;BYHOUR=8;BYMINUTE=0`).
 * The time-of-day is encoded via `BYHOUR`/`BYMINUTE` (no DTSTART) so the rule
 * carries no absolute anchor and the server localizes it in the task's tz.
 */
export function buildRRule(model: ScheduleModel): string {
  const { hour, minute } = model;
  switch (model.preset) {
    case "hourly":
      // Every hour on the minute — meets the 1h floor exactly.
      return "FREQ=HOURLY;BYMINUTE=0";
    case "daily":
      return `FREQ=DAILY;BYHOUR=${hour};BYMINUTE=${minute}`;
    case "weekdays":
      return `FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=${hour};BYMINUTE=${minute}`;
    case "weekly": {
      const byday = normalizeWeekdays(model.weekdays).join(",") || "MO";
      return `FREQ=WEEKLY;BYDAY=${byday};BYHOUR=${hour};BYMINUTE=${minute}`;
    }
    case "custom":
      return buildCustomRRule(model);
    default:
      return `FREQ=DAILY;BYHOUR=${hour};BYMINUTE=${minute}`;
  }
}

function buildCustomRRule(model: ScheduleModel): string {
  const { hour, minute } = model;
  const interval = clampInterval(model.interval);
  switch (model.customFreq) {
    case "hourly":
      // every X hours at minute Y
      return `FREQ=HOURLY;INTERVAL=${interval};BYMINUTE=${minute}`;
    case "daily":
      return `FREQ=DAILY;INTERVAL=${interval};BYHOUR=${hour};BYMINUTE=${minute}`;
    case "weekly": {
      const byday = normalizeWeekdays(model.weekdays).join(",") || "MO";
      return `FREQ=WEEKLY;INTERVAL=${interval};BYDAY=${byday};BYHOUR=${hour};BYMINUTE=${minute}`;
    }
    case "monthly": {
      const days = normalizeMonthDays(model.monthDays).join(",") || "1";
      return `FREQ=MONTHLY;INTERVAL=${interval};BYMONTHDAY=${days};BYHOUR=${hour};BYMINUTE=${minute}`;
    }
    case "yearly": {
      const days = normalizeMonthDays(model.monthDays).join(",") || "1";
      const month = clampMonth(model.month);
      return `FREQ=YEARLY;INTERVAL=${interval};BYMONTH=${month};BYMONTHDAY=${days};BYHOUR=${hour};BYMINUTE=${minute}`;
    }
    default:
      return `FREQ=DAILY;INTERVAL=${interval};BYHOUR=${hour};BYMINUTE=${minute}`;
  }
}

/**
 * Validate the schedule model against the form's constraints. Returns a
 * human-readable error string when invalid (shown inline + gates submit), or
 * `null` when the model is OK. Mirrors the server's rejection reasons so a bad
 * schedule fails fast in the form rather than as a 400.
 */
export function validateSchedule(model: ScheduleModel): string | null {
  // Multi-selects must have at least one selection.
  if (model.preset === "weekly" && model.weekdays.length === 0) {
    return "Pick at least one day of the week.";
  }
  if (model.preset === "custom") {
    if (!Number.isInteger(model.interval) || model.interval < 1) {
      return "Interval must be a whole number of at least 1.";
    }
    // 1-hour floor: a sub-hour cadence is only reachable via Custom Hourly with
    // INTERVAL < 1, which the interval check above already blocks. Guard the
    // hourly case explicitly for a clear message.
    if (model.customFreq === "hourly" && model.interval < 1) {
      return "Hourly tasks must run at most once per hour (interval ≥ 1).";
    }
    if (model.customFreq === "weekly" && model.weekdays.length === 0) {
      return "Pick at least one day of the week.";
    }
    if (
      (model.customFreq === "monthly" || model.customFreq === "yearly") &&
      model.monthDays.length === 0
    ) {
      return "Pick at least one day of the month.";
    }
  }
  return null;
}

/** Sort weekday codes into calendar order (MO..SU), dropping duplicates. */
function normalizeWeekdays(codes: WeekdayCode[]): WeekdayCode[] {
  const seen = new Set(codes);
  return WEEKDAY_CODES.filter((c) => seen.has(c));
}

/**
 * Sort day-of-month values ascending, drop dups, and clamp each to 1–31. We
 * allow the full 1–31 range (not a conservative ≤28) and let the server / rrule
 * handle short months — a `BYMONTHDAY=31` simply doesn't fire in February,
 * which is the standard RRULE semantic and matches user expectations.
 */
function normalizeMonthDays(days: number[]): number[] {
  const cleaned = days.map((d) => clampMonthDay(d)).filter((d): d is number => d != null);
  return [...new Set(cleaned)].sort((a, b) => a - b);
}

function clampInterval(n: number): number {
  if (!Number.isFinite(n)) return 1;
  return Math.max(1, Math.trunc(n));
}

function clampMonthDay(day: number): number | null {
  if (!Number.isFinite(day)) return null;
  const d = Math.trunc(day);
  if (d < 1 || d > 31) return null;
  return d;
}

function clampMonth(m: number): number {
  if (!Number.isFinite(m)) return 1;
  return Math.min(12, Math.max(1, Math.trunc(m)));
}
