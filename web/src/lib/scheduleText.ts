// Client-side derivation of the human-readable schedule summary the Tasks list
// shows for each task ("Weekdays at 8:00 AM"), computed from the stored
// RFC-5545 RRULE. `describeSchedule` is always correct because it only restates
// the rule.
//
// `nextRunAtMs` also computes the next-occurrence instant from the RRULE + IANA
// timezone, but it is NOT rendered as a user-facing countdown (see below) — we
// don't display a "Next run in Xh" number because there is no stable server
// anchor to match for INTERVAL>1 rules (the server anchors to the query day;
// this uses a fixed dtstart), so a shown countdown could be off by a period.
// It survives ONLY as the sort key for ordering active tasks by soonest run in
// TasksPage, where an approximate relative ordering is acceptable.
//
// We use the `rrule` library only for the next-occurrence math (parsing the
// rule and stepping to the next firing). The summary text is hand-formatted:
// rrule's own `.toText()` produces "every week on Monday, Tuesday, …" which is
// verbose and doesn't collapse the weekday set to "Weekdays" the way the design
// calls for.

import { RRule, rrulestr } from "rrule";

// A fixed, far-past UTC anchor for occurrence generation. The scheduled-task
// RRULEs carry no DTSTART (the time-of-day lives in BYHOUR/BYMINUTE), and
// without an explicit dtstart rrule.js defaults the anchor to the module's
// import-time `new Date()`, which would make `.after` generate occurrences from
// today rather than honoring the caller's `now`. Anchoring to a stable past
// date makes BY* rules fully determine the schedule. Matches the server's
// fixed-deterministic-anchor approach.
const OCCURRENCE_ANCHOR = new Date(Date.UTC(2000, 0, 1, 0, 0, 0));

/** Days-of-week bit set helpers. RRule weekday order is MO..SU (0..6). */
const WEEKDAYS = [RRule.MO, RRule.TU, RRule.WE, RRule.TH, RRule.FR].map((d) => d.weekday);
const ALL_DAYS = [RRule.MO, RRule.TU, RRule.WE, RRule.TH, RRule.FR, RRule.SA, RRule.SU].map(
  (d) => d.weekday,
);

const DAY_LABELS: Record<number, string> = {
  [RRule.MO.weekday]: "Monday",
  [RRule.TU.weekday]: "Tuesday",
  [RRule.WE.weekday]: "Wednesday",
  [RRule.TH.weekday]: "Thursday",
  [RRule.FR.weekday]: "Friday",
  [RRule.SA.weekday]: "Saturday",
  [RRule.SU.weekday]: "Sunday",
};

/**
 * Format an hour/minute pair as a 12-hour clock time, e.g. `8:00 AM`,
 * `12:30 PM`. Falls back to `midnight` semantics via the standard 12-hour
 * convention (0 → 12 AM).
 */
export function formatClockTime(hour: number, minute: number): string {
  const period = hour < 12 ? "AM" : "PM";
  const h12 = hour % 12 === 0 ? 12 : hour % 12;
  const mm = minute.toString().padStart(2, "0");
  return `${h12}:${mm} ${period}`;
}

/** Parse an RRULE string into an `RRule`, or `null` if it can't be parsed. */
function tryParse(rrule: string): RRule | null {
  try {
    const parsed = rrulestr(rrule);
    // rrulestr can return an RRuleSet for multi-rule strings; the scheduled-task
    // backend stores a single RRULE, so we only handle the RRule case for text.
    return parsed instanceof RRule ? parsed : null;
  } catch {
    return null;
  }
}

const MONTH_NAMES = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];

/**
 * Human-readable summary of an RRULE's recurrence. Handles every shape the
 * create form can produce — the simple presets and all five Custom
 * frequencies, including INTERVAL (>1), multi-day BYMONTHDAY, and yearly
 * BYMONTH — e.g.:
 *   - "Weekdays at 8:00 AM"
 *   - "Every day at 9:00 AM"
 *   - "Weekly on Monday and Friday at 2:30 PM"
 *   - "Every 2 hours at :15"
 *   - "Every 3 months on the 1st and 15th at 6:00 AM"
 *   - "Every 2 years in March on the 1st at 9:00 AM"
 *
 * Falls back to the library's `.toText()` (then the raw rule) for anything this
 * formatter doesn't special-case, so a valid-but-unusual rule still renders
 * something readable rather than blank.
 */
export function describeSchedule(rrule: string): string {
  const rule = tryParse(rrule);
  if (!rule) return rrule;
  const o = rule.origOptions;
  // BYHOUR/BYMINUTE come back as number | number[] | null | undefined.
  const hour = firstOf(o.byhour) ?? 0;
  const minute = firstOf(o.byminute) ?? 0;
  const timeSuffix = ` at ${formatClockTime(hour, minute)}`;
  const days = normalizeWeekdays(o.byweekday);
  const interval = typeof o.interval === "number" && o.interval > 1 ? o.interval : 1;

  if (o.freq === RRule.HOURLY) {
    // Time-of-day is meaningless hourly; show the minute offset instead.
    const min = `:${minute.toString().padStart(2, "0")}`;
    if (interval > 1) return `Every ${interval} hours at ${min}`;
    // interval 1: bare "Hourly" only when it fires on the hour; otherwise show
    // the non-zero BYMINUTE so ":30" isn't silently dropped.
    return minute === 0 ? "Hourly" : `Hourly at ${min}`;
  }

  if (o.freq === RRule.DAILY) {
    if (interval > 1) return `Every ${interval} days${timeSuffix}`;
    return `Every day${timeSuffix}`;
  }

  if (o.freq === RRule.WEEKLY) {
    const dayText = weekdayText(days);
    if (interval > 1) {
      // "Every 2 weeks on Monday…" — a named weekday set reads better than the
      // Weekdays/Weekends shorthands once an interval is involved.
      const on = days.length > 0 ? ` on ${dayLabelList(days)}` : "";
      return `Every ${interval} weeks${on}${timeSuffix}`;
    }
    if (days.length === 0) return `Weekly${timeSuffix}`;
    return `${dayText}${timeSuffix}`;
  }

  if (o.freq === RRule.MONTHLY) {
    const monthDays = normalizeNums(o.bymonthday);
    const on = monthDays.length > 0 ? ` on the ${ordinalList(monthDays)}` : "";
    if (interval > 1) return `Every ${interval} months${on}${timeSuffix}`;
    return `Monthly${on}${timeSuffix}`;
  }

  if (o.freq === RRule.YEARLY) {
    const monthNums = normalizeNums(o.bymonth);
    const monthDays = normalizeNums(o.bymonthday);
    const inMonth =
      monthNums.length > 0
        ? ` in ${monthNums
            .map((m) => MONTH_NAMES[m - 1])
            .filter(Boolean)
            .join(", ")}`
        : "";
    const on = monthDays.length > 0 ? ` on the ${ordinalList(monthDays)}` : "";
    if (interval > 1) return `Every ${interval} years${inMonth}${on}${timeSuffix}`;
    return `Yearly${inMonth}${on}${timeSuffix}`;
  }

  // Anything else: defer to the library's text, then the raw rule.
  try {
    return capitalize(rule.toText());
  } catch {
    return rrule;
  }
}

/** Weekday-set phrasing for the interval-1 weekly case (with shorthands). */
function weekdayText(days: number[]): string {
  if (sameSet(days, WEEKDAYS)) return "Weekdays";
  if (sameSet(days, ALL_DAYS)) return "Every day";
  if (sameSet(days, [RRule.SA.weekday, RRule.SU.weekday])) return "Weekends";
  return `Weekly on ${dayLabelList(days)}`;
}

function dayLabelList(days: number[]): string {
  return joinList(days.map((d) => DAY_LABELS[d]).filter(Boolean));
}

/**
 * The next firing of `rrule` after `now`, as an epoch-ms timestamp, or `null`
 * when the rule has no future occurrence (or can't be parsed).
 *
 * Timezone handling: the backend evaluates the rule in the task's IANA
 * `timezone`, but the `rrule` lib computes in "floating" wall-clock time with no
 * zone. We compute the next occurrence in the rule's own frame, then shift it by
 * the offset between the task timezone and the viewer's local zone at that
 * instant, so "8:00 AM America/New_York" resolves to the correct absolute
 * moment regardless of where the viewer sits. This mirrors how the server's
 * `python-dateutil` evaluation localizes the rule.
 */
export function nextRunAtMs(
  rrule: string,
  timezone: string,
  now: Date = new Date(),
): number | null {
  const base = tryParse(rrule);
  if (!base) return null;
  // Rebuild with a fixed dtstart so occurrence generation is anchored
  // deterministically (see OCCURRENCE_ANCHOR) rather than to import-time now.
  let rule: RRule;
  try {
    rule = new RRule({ ...base.origOptions, dtstart: OCCURRENCE_ANCHOR });
  } catch {
    return null;
  }
  // rrule's `.after` treats the DTSTART/occurrences as UTC-naive wall times.
  // Query against a "now" that is itself the current wall time in the task's
  // timezone, so the comparison happens in the rule's frame.
  const nowInTz = shiftWallClock(now, timezone);
  let occurrence: Date | null;
  try {
    occurrence = rule.after(nowInTz, false);
  } catch {
    return null;
  }
  if (!occurrence) return null;
  // `occurrence` is a wall-clock time in the task timezone, expressed as a naive
  // Date (its UTC fields hold the tz-local Y/M/D H:M). Convert back to a real
  // absolute instant by subtracting the tz offset at that wall time.
  return wallClockToInstantMs(occurrence, timezone);
}

// ── internals ────────────────────────────────────────────────────────────────

function firstOf(v: number | number[] | null | undefined): number | null {
  if (v == null) return null;
  return Array.isArray(v) ? (v.length > 0 ? v[0]! : null) : v;
}

/** Normalize a scalar-or-array RRULE option (e.g. bymonthday, bymonth) to a
 * sorted, de-duped number[]. */
function normalizeNums(v: number | number[] | null | undefined): number[] {
  if (v == null) return [];
  const arr = Array.isArray(v) ? v : [v];
  const nums = arr.filter((n): n is number => typeof n === "number");
  return [...new Set(nums)].sort((a, b) => a - b);
}

/** English ordinal for a day-of-month, e.g. 1 → "1st", 22 → "22nd". */
function ordinal(n: number): string {
  const mod100 = n % 100;
  if (mod100 >= 11 && mod100 <= 13) return `${n}th`;
  switch (n % 10) {
    case 1:
      return `${n}st`;
    case 2:
      return `${n}nd`;
    case 3:
      return `${n}rd`;
    default:
      return `${n}th`;
  }
}

/** "1st", "1st and 15th", "1st, 15th, and 28th". */
function ordinalList(days: number[]): string {
  return joinList(days.map(ordinal));
}

/**
 * Normalize rrule's `byweekday` (which can be a Weekday, number, or arrays of
 * either, in any order) into a sorted array of weekday indices (0=MO..6=SU).
 */
function normalizeWeekdays(byweekday: RRule["origOptions"]["byweekday"]): number[] {
  if (byweekday == null) return [];
  const arr = Array.isArray(byweekday) ? byweekday : [byweekday];
  const nums = arr
    .map((d) => (typeof d === "number" ? d : (d as { weekday: number }).weekday))
    .filter((n): n is number => typeof n === "number");
  return [...new Set(nums)].sort((a, b) => a - b);
}

function sameSet(a: number[], b: number[]): boolean {
  if (a.length !== b.length) return false;
  const sb = [...b].sort((x, y) => x - y);
  const sa = [...a].sort((x, y) => x - y);
  return sa.every((v, i) => v === sb[i]);
}

function joinList(items: string[]): string {
  if (items.length <= 1) return items[0] ?? "";
  if (items.length === 2) return `${items[0]} and ${items[1]}`;
  return `${items.slice(0, -1).join(", ")}, and ${items[items.length - 1]}`;
}

function capitalize(s: string): string {
  return s.length === 0 ? s : s[0]!.toUpperCase() + s.slice(1);
}

/**
 * The offset in minutes between UTC and `timezone` at instant `date`
 * (positive when the zone is behind UTC, matching `Date.getTimezoneOffset`).
 * Uses `Intl` so it's DST-correct for the given instant.
 */
function tzOffsetMinutes(date: Date, timezone: string): number {
  try {
    const dtf = new Intl.DateTimeFormat("en-US", {
      timeZone: timezone,
      hour12: false,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    const parts = dtf.formatToParts(date);
    const map: Record<string, number> = {};
    for (const p of parts) {
      if (p.type !== "literal") map[p.type] = Number(p.value);
    }
    // Wall-clock time in the target zone, as if it were UTC.
    const asUtc = Date.UTC(
      map.year!,
      map.month! - 1,
      map.day!,
      map.hour === 24 ? 0 : map.hour!,
      map.minute!,
      map.second!,
    );
    return (date.getTime() - asUtc) / 60_000;
  } catch {
    return 0;
  }
}

/**
 * Represent `date` as the naive wall-clock time in `timezone` (a Date whose UTC
 * fields hold the zone-local Y/M/D H:M:S), so it can be fed to rrule's
 * zone-naive `.after`.
 */
function shiftWallClock(date: Date, timezone: string): Date {
  const offset = tzOffsetMinutes(date, timezone);
  return new Date(date.getTime() - offset * 60_000);
}

/**
 * Inverse of {@link shiftWallClock}: given a naive wall-clock Date in
 * `timezone`, return the real absolute instant (epoch ms).
 */
function wallClockToInstantMs(wallDate: Date, timezone: string): number {
  // The offset at the wall instant is a close approximation of the offset at the
  // real instant; a single correction pass handles the common case. (DST-edge
  // firings are within an hour, well inside the "Xh" rounding the label uses.)
  const offset = tzOffsetMinutes(wallDate, timezone);
  return wallDate.getTime() + offset * 60_000;
}
