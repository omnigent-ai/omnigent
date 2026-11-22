// The sidebar's pull request filter, kept on this device like the owner filter beside it, so
// a reload shows the same sessions.

import {
  DEFAULT_SESSION_PULL_REQUEST_FILTER,
  SESSION_PULL_REQUEST_FILTERS,
  type SessionPullRequestFilter,
} from "./sessionListFilters";

const STORAGE_KEY = "omnigent:session-pull-request-filter";

/** The stored filter, or the default when none is stored, it's unknown, or storage fails. */
export function readSessionPullRequestFilter(): SessionPullRequestFilter {
  if (typeof window === "undefined") return DEFAULT_SESSION_PULL_REQUEST_FILTER;
  let raw: string | null;
  try {
    raw = window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return DEFAULT_SESSION_PULL_REQUEST_FILTER;
  }
  return (
    SESSION_PULL_REQUEST_FILTERS.find((filter) => filter === raw) ??
    DEFAULT_SESSION_PULL_REQUEST_FILTER
  );
}

/** Store the filter; a failed write keeps the sidebar working. */
export function writeSessionPullRequestFilter(value: SessionPullRequestFilter): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, value);
  } catch {
    // A local view preference; losing it is harmless.
  }
}
