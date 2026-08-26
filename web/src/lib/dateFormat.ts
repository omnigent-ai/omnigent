// Cached `Intl` formatters for the date/time labels the chat renders in bulk.
//
// `date.toLocaleTimeString(undefined, opts)` builds a fresh
// `Intl.DateTimeFormat` on every call, and constructing one is roughly 30x the
// cost of formatting with an existing instance (~46us vs ~1.5us). The message
// list formats a timestamp per bubble and re-formats them all whenever the list
// re-renders, so the constructor cost dominated the label.
//
// Caching at module scope is safe: a browser cannot change its locale without a
// reload, and `undefined` locales resolves once to the same default either way.

/** `9:41 AM` */
const timeFormat = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });

/** `Mar 4` */
const monthDayFormat = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });

/** `Mar 4, 2024` */
const monthDayYearFormat = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  year: "numeric",
});

/** Clock time, e.g. `9:41 AM`. */
export function formatTime(date: Date): string {
  return timeFormat.format(date);
}

/** Day within the current year, e.g. `Mar 4`. */
export function formatMonthDay(date: Date): string {
  return monthDayFormat.format(date);
}

/** Day in an earlier year, e.g. `Mar 4, 2024`. */
export function formatMonthDayYear(date: Date): string {
  return monthDayYearFormat.format(date);
}

/** True when both instants fall on the same local calendar day. */
export function isSameLocalDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}
