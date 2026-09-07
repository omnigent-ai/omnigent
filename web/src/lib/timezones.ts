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
 * NOTE: not currently called — v1 infers the timezone from the browser
 * (`localTimezone`) with no visible picker. Retained for the future
 * cross-timezone picker (see the comment in CreateScheduledTaskDialog).
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
