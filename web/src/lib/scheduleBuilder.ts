// Visual recurrence model shared by the automation form and RRULE serialization.
import { RRule } from "rrule";

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
  /** Omitted for schedules that never end; UNTIL is an RFC-5545 UTC instant. */
  end?: { type: "count"; count: number } | { type: "until"; until: string };
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
  const rule = buildRecurringRule(model);
  if (model.end?.type === "count") return `${rule};COUNT=${model.end.count}`;
  if (model.end?.type === "until") return `${rule};UNTIL=${model.end.until}`;
  return rule;
}

function buildRecurringRule(model: ScheduleModel): string {
  const { hour, minute } = model;
  switch (model.preset) {
    case "hourly":
      // Honor the snapped minute while keeping one fire per hour.
      return `FREQ=HOURLY;BYMINUTE=${minute}`;
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

export function parseRRuleToScheduleModel(rrule: string): ScheduleModel | null {
  const parts = parseRuleParts(rrule);
  if (parts === null) return null;
  const frequency = parts.get("FREQ")?.toLowerCase();
  if (!["hourly", "daily", "weekly", "monthly", "yearly"].includes(frequency ?? "")) return null;
  const customFreq = frequency as CustomFreq;
  const interval = parseNumberPart(parts.get("INTERVAL") ?? "1", 1, 366);
  const minute = parseNumberPart(parts.get("BYMINUTE"), 0, 59);
  const hour =
    customFreq === "hourly"
      ? DEFAULT_SCHEDULE_MODEL.hour
      : parseNumberPart(parts.get("BYHOUR"), 0, 23);
  if (interval === null || hour === null || minute === null) return null;
  const allowed = ["FREQ", "INTERVAL", "BYMINUTE", "COUNT", "UNTIL"];
  if (customFreq !== "hourly") allowed.push("BYHOUR");
  if (customFreq === "weekly") allowed.push("BYDAY");
  if (customFreq === "monthly" || customFreq === "yearly") allowed.push("BYMONTHDAY");
  if (customFreq === "yearly") allowed.push("BYMONTH");
  if (hasUnsupportedParts(parts, allowed)) return null;

  const model: ScheduleModel = {
    ...DEFAULT_SCHEDULE_MODEL,
    preset: "custom",
    customFreq,
    interval,
    hour,
    minute,
  };
  if (customFreq === "weekly") {
    const weekdays = parseWeekdaysPart(parts.get("BYDAY"));
    if (!weekdays?.length) return null;
    model.weekdays = weekdays;
  }
  if (customFreq === "monthly" || customFreq === "yearly") {
    const days = parts
      .get("BYMONTHDAY")
      ?.split(",")
      .map((day) => parseNumberPart(day, 1, 31));
    if (!days?.length || days.some((day) => day === null)) return null;
    model.monthDays = normalizeMonthDays(days as number[]);
  }
  if (customFreq === "yearly") {
    const month = parseNumberPart(parts.get("BYMONTH"), 1, 12);
    if (month === null) return null;
    model.month = month;
  }
  if (interval === 1 && ["hourly", "daily", "weekly"].includes(customFreq)) {
    model.preset = customFreq as SchedulePreset;
    if (customFreq === "weekly" && model.weekdays.join(",") === "MO,TU,WE,TH,FR")
      model.preset = "weekdays";
  }
  if (parts.has("COUNT") && parts.has("UNTIL")) return null;
  if (parts.has("COUNT")) {
    const count = parseNumberPart(parts.get("COUNT"), 2, 2147483647);
    if (count === null) return null;
    model.end = { type: "count", count };
  }
  if (parts.has("UNTIL")) {
    const until = parts.get("UNTIL")!;
    if (!parseUntil(until)) return null;
    model.end = { type: "until", until };
  }
  return validateSchedule(model, { allowPastEnd: true }) === null ? model : null;
}

/** Parse a UTC RRULE cutoff without allowing Date to normalize invalid dates. */
export function parseUntil(until: string): Date | null {
  if (!/^\d{8}T\d{6}Z$/.test(until)) return null;
  const date = new Date(
    `${until.slice(0, 4)}-${until.slice(4, 6)}-${until.slice(6, 8)}T${until.slice(9, 11)}:${until.slice(11, 13)}:${until.slice(13, 15)}Z`,
  );
  return Number.isFinite(date.getTime()) && formatUntil(date) === until ? date : null;
}

export function formatUntil(date: Date): string {
  return date
    .toISOString()
    .replace(/[-:]/g, "")
    .replace(/\.\d{3}Z$/, "Z");
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

function parseRuleParts(rrule: string): Map<string, string> | null {
  const body = rrule.startsWith("RRULE:") ? rrule.slice("RRULE:".length) : rrule;
  const parts = new Map<string, string>();
  for (const segment of body.split(";")) {
    const [rawKey, rawValue, extra] = segment.split("=");
    if (
      !rawKey ||
      rawValue == null ||
      rawValue === "" ||
      extra !== undefined ||
      parts.has(rawKey.toUpperCase())
    )
      return null;
    parts.set(rawKey.toUpperCase(), rawValue.toUpperCase());
  }
  return parts;
}

function hasUnsupportedParts(parts: Map<string, string>, supported: string[]): boolean {
  const supportedSet = new Set(supported);
  for (const key of parts.keys()) {
    if (!supportedSet.has(key)) return true;
  }
  return false;
}

function parseNumberPart(value: string | undefined, min: number, max: number): number | null {
  if (value == null) return null;
  if (!/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < min || parsed > max) return null;
  return parsed;
}

function parseWeekdaysPart(value: string | undefined): WeekdayCode[] | null {
  if (value == null) return null;
  const rawCodes = value.split(",");
  if (rawCodes.length === 0) return null;
  const codes: WeekdayCode[] = [];
  for (const rawCode of rawCodes) {
    if (!WEEKDAY_CODES.includes(rawCode as WeekdayCode)) return null;
    codes.push(rawCode as WeekdayCode);
  }
  return normalizeWeekdays(codes);
}

/** Validate visual form constraints; the server remains authoritative for saved rules. */
export function validateSchedule(
  model: ScheduleModel,
  { allowPastEnd = false, now = new Date() }: { allowPastEnd?: boolean; now?: Date } = {},
): string | null {
  if (!Number.isInteger(model.minute) || model.minute < 0 || model.minute > 59) {
    return model.preset === "hourly" ? "Enter a valid minute from 0 to 59." : "Enter a valid time.";
  }
  if (
    !(model.preset === "hourly" || (model.preset === "custom" && model.customFreq === "hourly")) &&
    (!Number.isInteger(model.hour) || model.hour < 0 || model.hour > 23)
  ) {
    return "Enter a valid time.";
  }

  // Multi-selects must have at least one selection.
  if (model.preset === "weekly" && model.weekdays.length === 0) {
    return "Pick at least one day of the week.";
  }
  if (model.preset === "custom") {
    if (!Number.isInteger(model.interval) || model.interval < 1 || model.interval > 366) {
      return "Interval must be a whole number of at least 1 and at most 366.";
    }
    if (model.customFreq === "weekly" && model.weekdays.length === 0) {
      return "Pick at least one day of the week.";
    }
    if (model.customFreq === "monthly" || model.customFreq === "yearly") {
      if (model.monthDays.length === 0) return "Pick at least one day of the month.";
      if (model.monthDays.some((day) => !Number.isInteger(day) || day < 1 || day > 31))
        return "Days of the month must be whole numbers from 1 to 31.";
      if (new Set(model.monthDays).size > 24) return "Pick at most 24 days of the month.";
    }
  }
  if (
    model.preset === "custom" &&
    model.customFreq === "yearly" &&
    (!Number.isInteger(model.month) || model.month < 1 || model.month > 12)
  )
    return "Pick a valid month.";
  if (
    model.end?.type === "count" &&
    (!Number.isInteger(model.end.count) || model.end.count < 2 || model.end.count > 2147483647)
  )
    return "Enter at least 2 occurrences (at most 2,147,483,647).";
  if (model.end?.type === "until") {
    const until = parseUntil(model.end.until);
    if (!until) return "Choose a valid end date.";
    if (!allowPastEnd && until <= now) return "Choose an end date in the future.";
  }
  if (model.preset === "custom" && model.customFreq === "yearly") {
    const daysInMonth = new Date(Date.UTC(2016, model.month, 0)).getUTCDate();
    if (!model.monthDays.some((day) => day <= daysInMonth))
      return "Pick a day that exists in the selected month.";
  }
  if (
    model.preset === "custom" &&
    (model.customFreq === "weekly" ||
      model.customFreq === "monthly" ||
      model.customFreq === "yearly")
  ) {
    // Match the server's fixed validation window without excluding multiple dates in one period.
    const occurrences = new RRule({
      ...RRule.parseString(buildRecurringRule(model)),
      dtstart: new Date(Date.UTC(2016, 0, 1)),
      until: new Date(Date.UTC(2024, 0, 1)),
      count: 2,
    }).all();
    if (occurrences.length < 2)
      return "Choose a recurrence that runs at least twice within eight years.";
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

export function parseTimeOfDayInput(input: string): { hour: number; minute: number } | null {
  const value = input.trim().toUpperCase().replace(/\s+/g, " ");
  const match = value.match(/^(\d{1,2})(?::(\d{1,2}))?\s*(AM|PM)?$/);
  if (!match) return null;

  const rawHour = Number(match[1]);
  const minute = match[2] == null ? 0 : Number(match[2]);
  const meridiem = match[3];
  if (!Number.isInteger(rawHour) || !Number.isInteger(minute) || minute < 0 || minute > 59) {
    return null;
  }

  if (meridiem != null) {
    if (rawHour < 1 || rawHour > 12) return null;
    const hour = (rawHour % 12) + (meridiem === "PM" ? 12 : 0);
    return { hour, minute };
  }

  if (rawHour < 0 || rawHour > 23) return null;
  return { hour: rawHour, minute };
}
