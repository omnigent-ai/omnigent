// Persisted, per-device preference for which Inbox tab is selected ("All" /
// "Unread" / "Awaiting response"), so a viewer who only wants approvals — or
// only new agent replies — lands on that view after a reload.

const STORAGE_KEY = "omnigent:inbox-filter";

/** Which slice of the Inbox is shown. */
export type InboxFilter = "all" | "unread" | "awaiting";

export const DEFAULT_INBOX_FILTER: InboxFilter = "all";

const INBOX_FILTERS = new Set<string>(["all", "unread", "awaiting"]);

/** Return whether a string is a known Inbox tab value. */
export function isInboxFilter(value: string): value is InboxFilter {
  return INBOX_FILTERS.has(value);
}

/**
 * Read the persisted Inbox tab. Falls back to {@link DEFAULT_INBOX_FILTER}
 * when nothing (or an unknown value) is stored, or storage is inaccessible.
 */
export function readInboxFilter(): InboxFilter {
  if (typeof window === "undefined") return DEFAULT_INBOX_FILTER;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return raw !== null && isInboxFilter(raw) ? raw : DEFAULT_INBOX_FILTER;
  } catch {
    return DEFAULT_INBOX_FILTER;
  }
}

/** Persist the Inbox tab. Swallows quota/access errors. */
export function writeInboxFilter(value: InboxFilter): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, value);
  } catch {
    // A local view preference; losing it is harmless.
  }
}
