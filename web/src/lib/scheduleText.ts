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
import type { ScheduledTaskRunStatus } from "./scheduledTasksApi";

// A fixed, far-past UTC anchor for occurrence generation. The scheduled-task
// RRULEs carry no DTSTART (the time-of-day lives in BYHOUR/BYMINUTE), and
// without an explicit dtstart rrule.js defaults the anchor to the module's
// import-time `new Date()`, which would make `.after` generate occurrences from
// today rather than honoring the caller's `now`. Anchoring to a stable past
// date makes BY* rules fully determine the schedule. Matches the server's
// fixed-deterministic-anchor approach.
const OCCURRENCE_ANCHOR = new Date(Date.UTC(2000, 0, 1, 0, 0, 0));
const FIXED_PERIOD_SAFETY_MARGIN = 2;

// Millisecond units for the compact relative next-run delta (formatNextRunAt).
const MIN_MS = 60_000;
const HOUR_MS = 60 * MIN_MS;
const DAY_MS = 24 * HOUR_MS;

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

/**
 * Format the time until the SERVER's authoritative next-run instant as a
 * relative delta in full words, e.g. `in 8 mins`, `in 3 hours`, `in 2 days`,
 * or `soon`. This is the user-facing next-run label in the Tasks list.
 *
 * `iso` is the server's `next_run_at` (the live scheduler's authoritative next
 * fire). We compute ONLY the delta (`nextRunAt − now`) and format how far away
 * it is — the instant itself stays server-authoritative. This is NOT the old
 * client-recomputed countdown that was removed: that one recomputed WHICH
 * instant is next on the client (and couldn't match the server anchor for
 * INTERVAL>1 rules); here the instant comes from the server and only the
 * "how far away" is derived, so the removal rule is not violated.
 *
 * Buckets (rounded to the nearest of the chosen unit — the closest "about how
 * long" reading), with a singular noun when the value is exactly 1:
 *   < 60 min → `in N min(s)` (minimum `in 1 min`), < 24 h → `in N hour(s)`,
 *   else `in N day(s)`. Each threshold uses the already-rounded value so a delta
 *   that rounds up to a full unit promotes to the next unit (never `in 60 mins`
 *   or `in 24 hours`).
 * A delta at or below ~0 (imminent / just passed due to clock skew) → `soon`.
 * Returns `null` for a null / unparseable `iso` so the caller renders nothing.
 *
 * @param iso The server's `next_run_at` ISO string, or `null`.
 * @param now Injectable clock for deterministic tests; defaults to `new Date()`.
 */
export function formatNextRunAt(
  iso: string | null | undefined,
  now: Date = new Date(),
): string | null {
  if (!iso) return null;
  const instant = new Date(iso);
  if (Number.isNaN(instant.getTime())) return null;

  const deltaMs = instant.getTime() - now.getTime();
  // Imminent or just-passed (timing skew): don't render a "0 mins" / negative delta.
  if (deltaMs < MIN_MS) return "soon";
  // Round to nearest — the closest approximation of "about how long" (a 1h49m
  // delta reads "in 2 hours", not the floored "in 1 hour"). Each guard tests the
  // ALREADY-ROUNDED value so a delta that rounds up to a full unit PROMOTES to
  // the next unit rather than printing "in 60 mins" / "in 24 hours" (e.g. 59m40s
  // → "in 1 hour", 23h40m → "in 1 day").
  const mins = Math.round(deltaMs / MIN_MS);
  if (mins < 60) return `in ${pluralize(mins, "min")}`;
  const hours = Math.round(deltaMs / HOUR_MS);
  if (hours < 24) return `in ${pluralize(hours, "hour")}`;
  const days = Math.round(deltaMs / DAY_MS);
  return `in ${pluralize(days, "day")}`;
}

/** `1 min` / `8 mins` — singular noun only when the count is exactly 1. */
function pluralize(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

/**
 * Format the wall-clock time a run FIRED (or was scheduled) as a short absolute
 * label for the run-history list, e.g. `Today, 8:00 AM`, `Yesterday, 2:30 PM`,
 * or `Jul 26, 8:00 AM` for anything older. The date is relative to `now` by
 * calendar day (viewer-local), so "Today"/"Yesterday" track midnight rather than
 * a rolling 24h window.
 *
 * @param epochSeconds The run's `firedAt ?? scheduledAt` (epoch SECONDS, matching
 *   the server's `_run_to_response`), or `null`.
 * @param now Injectable clock for deterministic tests; defaults to `new Date()`.
 * @returns The label, or `null` for a null / non-finite input so the caller
 *   renders nothing.
 */
export function formatRunTimestamp(
  epochSeconds: number | null | undefined,
  now: Date = new Date(),
): string | null {
  if (epochSeconds == null || !Number.isFinite(epochSeconds)) return null;
  const date = new Date(epochSeconds * 1000);
  if (Number.isNaN(date.getTime())) return null;

  const time = date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  const dayDelta = calendarDayDelta(date, now);
  if (dayDelta === 0) return `Today, ${time}`;
  if (dayDelta === 1) return `Yesterday, ${time}`;
  // Older (or a future-dated run, e.g. a scheduled-but-not-fired row): show an
  // absolute month/day. Include the year only when it differs from `now`'s so a
  // run from a prior year isn't ambiguous.
  const sameYear = date.getFullYear() === now.getFullYear();
  const datePart = date.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    ...(sameYear ? {} : { year: "numeric" }),
  });
  return `${datePart}, ${time}`;
}

/**
 * Whole-calendar-day difference (viewer-local) between `date` and `now`:
 * `0` = same day, `1` = `date` was yesterday, negative = `date` is in the
 * future. Compares local midnights so it ignores time-of-day.
 */
function calendarDayDelta(date: Date, now: Date): number {
  const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  return Math.round((startOfDay(now) - startOfDay(date)) / DAY_MS);
}

/**
 * Format a run's elapsed wall-clock duration (`finishedAt − firedAt`) as a
 * compact `1m 42s` / `3s` / `2h 5m` label for the run-history list.
 *
 * Both inputs are epoch SECONDS (server `_run_to_response`). Returns `null` when
 * either bound is missing (the run hasn't finished, or never fired) or the
 * arithmetic is nonsensical (finished before fired / non-finite), so the caller
 * simply omits the duration rather than showing `0s` or a negative value.
 *
 * Units: shows the two most-significant non-zero units, largest first — `h`+`m`
 * for hour-scale runs, `m`+`s` for minute-scale, bare `s` under a minute. A
 * sub-second run rounds up to `1s` (a run that fired and finished always took
 * *some* time; `0s` reads as "didn't run").
 */
export function formatRunDuration(
  firedAt: number | null | undefined,
  finishedAt: number | null | undefined,
): string | null {
  if (firedAt == null || finishedAt == null) return null;
  if (!Number.isFinite(firedAt) || !Number.isFinite(finishedAt)) return null;
  const seconds = finishedAt - firedAt;
  if (seconds < 0) return null;
  const total = Math.max(1, Math.round(seconds));

  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return m > 0 ? `${h}h ${m}m` : `${h}h`;
  if (m > 0) return s > 0 ? `${m}m ${s}s` : `${m}m`;
  return `${s}s`;
}

/**
 * Where a run stopped and why, for the run history. `stage` names the step of
 * the fire that failed, so a failure can be attributed without reading logs;
 * it is `null` when the code doesn't identify one.
 */
export interface RunFailure {
  stage: string | null;
  reason: string;
}

// Keyed by the `error_code` the fire path, the persistent scheduler and the
// stale-run reconciler record (`omnigent/server/scheduled/`). Validation
// failures carry an `ErrorCode` value, e.g. `invalid_input`.
const RUN_FAILURES: Readonly<Record<string, RunFailure>> = {
  owner_deleted: { stage: "Owner", reason: "The owner's account was removed." },
  owner_identity_unavailable: {
    stage: "Owner",
    // Recurrence-neutral: a one-time automation, such as a reminder, has no next run.
    reason: "Couldn't act as the owner. A recurring automation tries again at its next run.",
  },
  reauthorization_required: {
    stage: "Owner",
    reason: "The owner's authorization for this automation is no longer valid.",
  },
  invalid_input: {
    stage: "Settings",
    reason: "The agent, model or workspace is invalid. Edit the automation to fix it.",
  },
  permission_harness_unresolved: {
    stage: "Permissions",
    reason: "Couldn't load the agent's permission settings. Restore access to the agent and retry.",
  },
  permission_mode_unsupported: {
    stage: "Permissions",
    reason:
      "The saved permission isn't supported by this agent. Edit the automation to change or clear it.",
  },
  not_found: {
    stage: "Settings",
    reason: "The agent or workspace no longer exists. Edit the automation to fix it.",
  },
  forbidden: { stage: "Settings", reason: "You no longer have access to the agent or workspace." },
  missing_host_id: {
    stage: "Settings",
    reason: "No host or workspace is set. Edit the automation to fix it.",
  },
  missing_workspace: {
    stage: "Settings",
    reason: "No host or workspace is set. Edit the automation to fix it.",
  },
  missing_execution_input: {
    stage: "Settings",
    reason: "No host or workspace is set. Edit the automation to fix it.",
  },
  unsupported_target: { stage: "Settings", reason: "This target can't run automatically." },
  no_online_host: { stage: "Host", reason: "None of your hosts was online." },
  host_offline: { stage: "Host", reason: "The host was offline." },
  host_not_found: { stage: "Host", reason: "The host no longer exists." },
  host_not_owned: { stage: "Host", reason: "The host isn't yours." },
  host_authority_revoked: { stage: "Host", reason: "The host was registered by another account." },
  host_registry_unavailable: { stage: "Host", reason: "The server couldn't look up hosts." },
  existing_sandbox_not_allowed: {
    stage: "Host",
    reason: "Automations can't use an existing sandbox. Choose a new sandbox for each run.",
  },
  default_workspace_unresolved: {
    stage: "Host",
    reason: "Couldn't find the host's home directory.",
  },
  managed_sandbox_unavailable: { stage: "Sandbox", reason: "This server has no sandboxes." },
  session_create_failed: { stage: "Session", reason: "Couldn't create the session." },
  owner_grant_failed: {
    stage: "Session",
    reason: "Couldn't give the owner access to the session.",
  },
  sandbox_create_failed: { stage: "Sandbox", reason: "Couldn't create the sandbox." },
  sandbox_registration_timeout: { stage: "Sandbox", reason: "The sandbox didn't connect in time." },
  runner_start_failed: { stage: "Runner", reason: "The agent didn't start." },
  launch_failed: { stage: "Launch", reason: "The agent session failed to launch." },
  startup_expired: { stage: "Startup", reason: "The run stopped before the agent started." },
  incomplete: { stage: "Running", reason: "The agent stopped reporting before the run finished." },
};

/**
 * Describe a run that did not succeed from its `errorCode`. An unknown or
 * missing code still gets a reason, worded from the run's status, so a code
 * added on the server later renders something rather than nothing.
 *
 * @param errorCode The run's `errorCode`, or `null`.
 * @param status The run's status, used only for the fallback wording.
 */
export function describeRunFailure(
  errorCode: string | null | undefined,
  status: ScheduledTaskRunStatus,
): RunFailure {
  if (errorCode != null && Object.hasOwn(RUN_FAILURES, errorCode)) return RUN_FAILURES[errorCode];
  return { stage: null, reason: status === "skipped" ? "Run skipped." : "Run failed." };
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
 * Human-readable summary of an RRULE's recurrence, e.g.:
 *   - "Weekdays at 8:00 AM"
 *   - "Every day at 9:00 AM and 5:00 PM"
 *   - "Hourly from 9:00 AM to 5:00 PM on weekdays"
 *   - "Every 15 minutes during 9:00 AM–5:00 PM on weekdays"
 *   - "Monthly on the first Monday at 9:00 AM"
 *   - "Every 2 years in March on the 1st at 9:00 AM"
 *
 * Rules with fields these phrases can't state exactly fall back to the library's
 * `.toText()` where it is faithful, then to the raw rule, so a summary never
 * describes a different schedule.
 */
export function describeSchedule(rrule: string, timezone?: string): string {
  const rule = tryParse(rrule);
  if (!rule) return rrule;
  const o = rule.origOptions;
  // BYHOUR/BYMINUTE come back as number | number[] | null | undefined.
  const hour = firstOf(o.byhour) ?? 0;
  const minute = firstOf(o.byminute) ?? 0;
  const timeSuffix = ` at ${formatClockTime(hour, minute)}`;

  // A one-time rule, such as a reminder, runs at its first match and then completes.
  if (o.count === 1) {
    const months = normalizeNums(o.bymonth);
    const monthDays = normalizeNums(o.bymonthday);
    const hours = normalizeNums(o.byhour);
    const minutes = normalizeNums(o.byminute);
    const monthName = months.length === 1 ? MONTH_NAMES[months[0]! - 1] : undefined;
    // A literal date only for the reminder shape: one month, one positive day and
    // one time. Anything else (negative days, several times) names no single moment.
    if (
      o.freq === RRule.YEARLY &&
      monthName !== undefined &&
      monthDays.length === 1 &&
      monthDays[0]! > 0 &&
      hours.length <= 1 &&
      minutes.length <= 1
    ) {
      // A reminder is stored in UTC, so name the zone its time is in.
      const zone = !timezone ? "" : timezone === "UTC" ? " UTC" : ` (${timezone})`;
      return `Once on ${monthName} ${monthDays[0]}${timeSuffix}${zone}`;
    }
    const recurring = rrule
      .replace(/^RRULE:/i, "")
      .split(";")
      .filter((part) => !/^\s*COUNT\s*=/i.test(part))
      .join(";");
    return `Once: ${describeSchedule(recurring)}`;
  }

  return summarizeRecurrence(o) ?? fallbackText(rule, rrule);
}

/** The phrase for a rule whose every field it can state, or null. */
function summarizeRecurrence(o: RRule["origOptions"]): string | null {
  const interval = typeof o.interval === "number" && o.interval > 1 ? o.interval : 1;
  if (o.byyearday != null || o.byweekno != null || o.byeaster != null) return null;
  if (normalizeNums(o.bysecond).some((second) => second !== 0)) return null;
  // WKST only changes which weeks a multi-week interval groups together.
  if (o.freq === RRule.WEEKLY && interval > 1 && o.wkst != null && weekdayIndex(o.wkst) !== 0)
    return null;
  const days = weekdayParts(o.byweekday);
  const hours = normalizeNums(o.byhour);
  const minutes = normalizeNums(o.byminute);
  const months = normalizeNums(o.bymonth);
  const monthDays = normalizeNums(o.bymonthday);
  const setPositions = normalizeNums(o.bysetpos);
  const monthly = o.freq === RRule.MONTHLY || o.freq === RRule.YEARLY;
  if (!monthly && (days.nth.length > 0 || months.length > 0 || monthDays.length > 0)) return null;
  if (setPositions.length > 0 && o.freq !== RRule.MONTHLY) return null;

  if (o.freq === RRule.MINUTELY) {
    if (minutes.length > 0) return null;
    const every = interval === 1 ? "Every minute" : `Every ${interval} minutes`;
    const during =
      hours.length > 0 && hours.length < 24 ? ` during ${formatHourRanges(hours)}` : "";
    return `${every}${during}${onDaysSuffix(days.plain)}`;
  }

  if (o.freq === RRule.HOURLY) {
    const on = onDaysSuffix(days.plain);
    const runMinutes = minutes.length > 0 ? minutes : [0];
    if (hours.length === 0 || hours.length === 24) {
      // Bare "Hourly" only when it fires on the hour, so ":30" isn't silently dropped.
      const at =
        interval === 1 && runMinutes.length === 1 && runMinutes[0] === 0
          ? ""
          : ` at ${joinList(runMinutes.map((value) => `:${value.toString().padStart(2, "0")}`))}`;
      return `${interval > 1 ? `Every ${interval} hours` : "Hourly"}${at}${on}`;
    }
    // INTERVAL with BYHOUR counts from the creation day's midnight; no phrase states that.
    if (interval > 1) return null;
    return capitalize(timesOfDayText(hours, runMinutes)) + on;
  }

  const times = timesOfDayText(hours.length > 0 ? hours : [0], minutes.length > 0 ? minutes : [0]);
  const listedTimes = timesOfDayText(
    hours.length > 0 ? hours : [0],
    minutes.length > 0 ? minutes : [0],
    false,
  );

  if (o.freq === RRule.DAILY) {
    if (days.plain.length > 0) {
      if (interval > 1) return null;
      return times.startsWith("at ")
        ? `${weekdayText(days.plain)} ${times}`
        : capitalize(times) + onDaysSuffix(days.plain);
    }
    if (interval > 1) return `Every ${interval} days ${listedTimes}`;
    return times.startsWith("at ") ? `Every day ${times}` : capitalize(times);
  }

  if (o.freq === RRule.WEEKLY) {
    if (interval > 1) {
      // "Every 2 weeks on Monday…" — a named weekday set reads better than the
      // Weekdays/Weekends shorthands once an interval is involved.
      const on = days.plain.length > 0 ? ` on ${dayLabelList(days.plain)}` : "";
      return `Every ${interval} weeks${on} ${listedTimes}`;
    }
    if (days.plain.length === 0) return `Weekly ${listedTimes}`;
    return times.startsWith("at ")
      ? `${weekdayText(days.plain)} ${times}`
      : capitalize(times) + onDaysSuffix(days.plain);
  }

  if (monthly) {
    const singleTime = Math.max(hours.length, 1) * Math.max(minutes.length, 1) === 1;
    const on = monthDayText(monthDays, days, setPositions, singleTime);
    if (on === null) return null;
    const inMonths =
      months.length > 0 ? ` in ${joinList(months.map((month) => MONTH_NAMES[month - 1]!))}` : "";
    const unit = o.freq === RRule.MONTHLY ? "month" : "year";
    const every =
      interval > 1 ? `Every ${interval} ${unit}s` : unit === "month" ? "Monthly" : "Yearly";
    return `${every}${inMonths}${on === "" ? "" : ` on the ${on}`} ${listedTimes}`;
  }

  return null;
}

/** The days of the month a monthly or yearly rule names, "" for none, or null. */
function monthDayText(
  monthDays: number[],
  days: ReturnType<typeof weekdayParts>,
  setPositions: number[],
  singleTime: boolean,
): string | null {
  const weekdays = days.plain.length > 0 || days.nth.length > 0;
  if (monthDays.length > 0) {
    if (weekdays || setPositions.length > 0 || monthDays.some((day) => day < -1)) return null;
    const ordered = [...monthDays.filter((day) => day > 0), ...monthDays.filter((day) => day < 0)];
    return joinList(ordered.map((day) => (day === -1 ? "last day" : ordinal(day))));
  }
  if (setPositions.length > 0) {
    // BYSETPOS picks among every run in the month, so it reads as one day only for one time.
    if (days.nth.length > 0 || days.plain.length === 0 || !singleTime) return null;
    const positions = positionWords(setPositions);
    let kind: string | null = null;
    if (days.plain.length === 1) kind = DAY_LABELS[days.plain[0]!] ?? null;
    else if (sameSet(days.plain, WEEKDAYS)) kind = "weekday";
    else if (sameSet(days.plain, [RRule.SA.weekday, RRule.SU.weekday])) kind = "weekend day";
    else if (sameSet(days.plain, ALL_DAYS)) kind = "day";
    return positions === null || kind === null ? null : `${positions} ${kind}`;
  }
  if (days.plain.length > 0) return null;
  if (days.nth.length === 0) return "";
  const byDay = new Map<number, number[]>();
  for (const { weekday, n } of days.nth) byDay.set(weekday, [...(byDay.get(weekday) ?? []), n]);
  const phrases: string[] = [];
  for (const [weekday, positions] of byDay) {
    const words = positionWords(positions);
    if (words === null) return null;
    phrases.push(`${words} ${DAY_LABELS[weekday]}`);
  }
  return joinList(phrases);
}

const POSITION_WORDS: Record<number, string> = {
  1: "first",
  2: "second",
  3: "third",
  4: "fourth",
  5: "fifth",
  [-1]: "last",
};

/** "first and third", or null for positions without a word (e.g. -2). */
function positionWords(positions: number[]): string | null {
  const ordered = [...positions.filter((n) => n > 0), ...positions.filter((n) => n < 0)];
  if (ordered.some((n) => POSITION_WORDS[n] === undefined)) return null;
  return joinList([...new Set(ordered)].map((n) => POSITION_WORDS[n]!));
}

/**
 * "at 9:00 AM and 5:00 PM", or for three or more evenly spaced hours at one minute
 * "hourly from 9:00 AM to 5:00 PM" / "every 4 hours from 9:00 AM to 5:00 PM".
 */
function timesOfDayText(hours: number[], minutes: number[], allowWindow = true): string {
  const sequence = allowWindow && minutes.length === 1 ? hourSequence(hours) : null;
  if (sequence !== null) {
    const cadence = sequence.step === 1 ? "hourly" : `every ${sequence.step} hours`;
    const first = formatClockTime(sequence.first, minutes[0]!);
    const last = formatClockTime(sequence.last, minutes[0]!);
    return `${cadence} from ${first} to ${last}`;
  }
  const times = hours.flatMap((hour) => minutes.map((value) => formatClockTime(hour, value)));
  return `at ${joinList(times)}`;
}

/** Three or more hours with one step between them, e.g. 9 AM to 5 PM every 4 hours. */
function hourSequence(hours: number[]): { first: number; last: number; step: number } | null {
  if (hours.length < 3 || hours.length === 24) return null;
  const ordered = fromWidestGap(hours);
  const step = (ordered[1]! - ordered[0]! + 24) % 24;
  const even = ordered.every(
    (hour, index) => index === 0 || (hour - ordered[index - 1]! + 24) % 24 === step,
  );
  return even ? { first: ordered[0]!, last: ordered[ordered.length - 1]!, step } : null;
}

/** "9:00 AM–5:00 PM" for the hours a minute rule runs during, joining runs of hours. */
function formatHourRanges(hours: number[]): string {
  const ranges: [number, number][] = [];
  for (const hour of fromWidestGap(normalizeNums(hours))) {
    const last = ranges[ranges.length - 1];
    if (last && (last[1] + 1) % 24 === hour) last[1] = hour;
    else ranges.push([hour, hour]);
  }
  return joinList(
    ranges.map(
      ([first, last]) => `${formatClockTime(first, 0)}–${formatClockTime((last + 1) % 24, 0)}`,
    ),
  );
}

/** Sorted hours rotated to start after the widest gap, so 10 PM to 6 AM stays in order. */
function fromWidestGap(hours: number[]): number[] {
  const gapBefore = (index: number) =>
    (hours[index]! - hours[(index + hours.length - 1) % hours.length]! + 24) % 24 || 24;
  let start = 0;
  for (let index = 1; index < hours.length; index++)
    if (gapBefore(index) > gapBefore(start)) start = index;
  return [...hours.slice(start), ...hours.slice(0, start)];
}

/** " on weekdays", " on weekends" or " on <days>"; empty when every day runs. */
function onDaysSuffix(days: number[]): string {
  if (days.length === 0 || sameSet(days, ALL_DAYS)) return "";
  if (sameSet(days, WEEKDAYS)) return " on weekdays";
  if (sameSet(days, [RRule.SA.weekday, RRule.SU.weekday])) return " on weekends";
  return ` on ${dayLabelList(days)}`;
}

/** Plain weekdays (MO) and numbered ones (1MO, -1FR) from rrule's `byweekday`. */
function weekdayParts(byweekday: RRule["origOptions"]["byweekday"]): {
  plain: number[];
  nth: { weekday: number; n: number }[];
} {
  const items = byweekday == null ? [] : Array.isArray(byweekday) ? byweekday : [byweekday];
  const plain = new Set<number>();
  const nth: { weekday: number; n: number }[] = [];
  for (const item of items) {
    if (typeof item === "object" && item.n) nth.push({ weekday: item.weekday, n: item.n });
    else plain.add(weekdayIndex(item));
  }
  return { plain: [...plain].sort((a, b) => a - b), nth };
}

function weekdayIndex(day: number | string | { weekday: number }): number {
  if (typeof day === "number") return day;
  if (typeof day === "string") return RRule[day as "MO"].weekday;
  return day.weekday;
}

/** The library's text where it states the rule faithfully, otherwise the rule itself. */
function fallbackText(rule: RRule, rrule: string): string {
  const o = rule.origOptions;
  // The library drops minute-rule filters and misreads BYSETPOS and BYEASTER.
  if (o.freq === RRule.MINUTELY || o.freq === RRule.SECONDLY) return rrule;
  if (o.bysetpos != null || o.byeaster != null || o.byhour != null) return rrule;
  try {
    return rule.isFullyConvertibleToText() ? capitalize(rule.toText()) : rrule;
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
  // rrule's `.after` treats the DTSTART/occurrences as UTC-naive wall times.
  // Query against a "now" that is itself the current wall time in the task's
  // timezone, so the comparison happens in the rule's frame.
  const nowInTz = shiftWallClock(now, timezone);
  // Rebuild with a fixed dtstart so occurrence generation is anchored
  // deterministically (see OCCURRENCE_ANCHOR) rather than to import-time now.
  // For fixed-period rules, move that anchor forward by whole periods so rrule
  // only iterates near "now" while preserving the original 2000-based phase.
  const dtstart = occurrenceAnchorNear(base, nowInTz);
  let rule: RRule;
  try {
    rule = new RRule({ ...base.origOptions, dtstart });
  } catch {
    return null;
  }
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

function occurrenceAnchorNear(rule: RRule, nowInRuleFrame: Date): Date {
  const periodMs = fixedPeriodMs(rule);
  if (periodMs == null) return OCCURRENCE_ANCHOR;

  const elapsedMs = nowInRuleFrame.getTime() - OCCURRENCE_ANCHOR.getTime();
  if (elapsedMs <= 0) return OCCURRENCE_ANCHOR;

  const periodsSinceAnchor = Math.floor(elapsedMs / periodMs);
  const periodsToAdvance = Math.max(0, periodsSinceAnchor - FIXED_PERIOD_SAFETY_MARGIN);
  return new Date(OCCURRENCE_ANCHOR.getTime() + periodsToAdvance * periodMs);
}

function fixedPeriodMs(rule: RRule): number | null {
  const interval =
    typeof rule.origOptions.interval === "number" && rule.origOptions.interval > 1
      ? rule.origOptions.interval
      : 1;

  switch (rule.origOptions.freq) {
    case RRule.SECONDLY:
      return interval * 1000;
    case RRule.MINUTELY:
      return interval * 60_000;
    case RRule.HOURLY:
      return interval * 3_600_000;
    case RRule.DAILY:
      return interval * 86_400_000;
    case RRule.WEEKLY:
      return interval * 604_800_000;
    default:
      return null;
  }
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
