// The sidebar's filters, kept on this device like the owner filter beside them, so a reload
// shows the same sessions.

import {
  DEFAULT_SESSION_LIST_FILTER,
  DEFAULT_SESSION_PULL_REQUEST_FILTER,
  parseSessionListFilter,
  SESSION_PULL_REQUEST_FILTERS,
  type SessionListFilter,
  type SessionPullRequestFilter,
} from "./sessionListFilters";

const STORAGE_KEY = "omnigent:session-pull-request-filter";
const LIST_FILTER_KEY = "omnigent:session-list-filter";

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

/**
 * The stored filters, each falling back to All on its own. Until they're first stored, the pull
 * request filter stored on its own carries over, so that choice survives the move.
 */
export function readSessionListFilter(): SessionListFilter {
  if (typeof window === "undefined") return DEFAULT_SESSION_LIST_FILTER;
  let raw: string | null;
  try {
    raw = window.localStorage.getItem(LIST_FILTER_KEY);
  } catch {
    return DEFAULT_SESSION_LIST_FILTER;
  }
  if (raw === null) {
    return { ...DEFAULT_SESSION_LIST_FILTER, pullRequest: readSessionPullRequestFilter() };
  }
  try {
    return parseSessionListFilter(JSON.parse(raw));
  } catch {
    return DEFAULT_SESSION_LIST_FILTER;
  }
}

/** Store the filters; a failed write keeps the sidebar working. */
export function writeSessionListFilter(value: SessionListFilter): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(LIST_FILTER_KEY, JSON.stringify(value));
  } catch {
    // A local view preference; losing it is harmless.
  }
}
