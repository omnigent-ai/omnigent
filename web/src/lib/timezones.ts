// Timezone options for the scheduled-task create form. Uses the platform's IANA
// zone list when available (`Intl.supportedValuesOf`), falling back to a small
// curated list on older engines that lack it. The server validates the chosen
// value against the full IANA database, so this list only needs to be usable,
// not exhaustive.

/** The viewer's local IANA timezone, or `UTC` if it can't be resolved. */
export function localTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

const FALLBACK_ZONES = [
  "UTC",
  "America/Los_Angeles",
  "America/Denver",
  "America/Chicago",
  "America/New_York",
  "America/Sao_Paulo",
  "Europe/London",
  "Europe/Berlin",
  "Europe/Paris",
  "Europe/Moscow",
  "Asia/Kolkata",
  "Asia/Singapore",
  "Asia/Shanghai",
  "Asia/Tokyo",
  "Australia/Sydney",
];

/**
 * The timezone options to offer, always including `UTC` and `ensure` (the
 * currently-selected value) so a preselected local/uncommon zone is never
 * missing from the list.
 *
 */
export function commonTimezones(ensure?: string): string[] {
  let zones: string[];
  try {
    // `supportedValuesOf` is ES2023; guard for older runtimes/tests.
    const supported = (
      Intl as unknown as { supportedValuesOf?: (key: string) => string[] }
    ).supportedValuesOf?.("timeZone");
    zones = supported && supported.length > 0 ? [...supported] : [...FALLBACK_ZONES];
  } catch {
    zones = [...FALLBACK_ZONES];
  }
  const set = new Set(zones);
  set.add("UTC");
  if (ensure) set.add(ensure);
  return [...set].sort((a, b) => {
    // UTC first, then alphabetical.
    if (a === "UTC") return -1;
    if (b === "UTC") return 1;
    return a.localeCompare(b);
  });
}

/** Calendar date in the schedule's timezone, independent of the browser's zone. */
export function dateInTimezone(date: Date, timezone: string): string {
  const formatter = calendarFormatter(timezone);
  if (!formatter || !Number.isFinite(date.getTime())) return "";
  return calendarDate(formatter, date);
}

/** Last second in the selected local date, including a repeated or shortened final hour. */
export function endOfDayInTimezone(day: string, timezone: string): Date | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return null;
  const target = Date.parse(`${day}T00:00:00Z`);
  if (!Number.isFinite(target) || new Date(target).toISOString().slice(0, 10) !== day) return null;
  const formatter = calendarFormatter(timezone);
  if (!formatter) return null;
  // Bracket all IANA offsets and search the date boundary, not a possibly ambiguous wall time.
  const secondsPerDay = 86400;
  const dateKey = Number(day.replaceAll("-", ""));
  let before = target / 1000 - 2 * secondsPerDay;
  let after = target / 1000 + 3 * secondsPerDay;
  while (after - before > 1) {
    const middle = Math.floor((before + after) / 2);
    const middleDateKey = Number(
      calendarDate(formatter, new Date(middle * 1000)).replaceAll("-", ""),
    );
    if (middleDateKey <= dateKey) before = middle;
    else after = middle;
  }
  const result = new Date(before * 1000);
  return calendarDate(formatter, result) === day ? result : null;
}

function calendarFormatter(timezone: string): Intl.DateTimeFormat | null {
  try {
    return new Intl.DateTimeFormat("en-CA", {
      timeZone: timezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });
  } catch {
    return null;
  }
}

function calendarDate(formatter: Intl.DateTimeFormat, date: Date): string {
  const parts = formatter.formatToParts(date);
  const value = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((part) => part.type === type)!.value;
  return `${value("year").padStart(4, "0")}-${value("month")}-${value("day")}`;
}
