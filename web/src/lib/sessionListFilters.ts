// Narrows the sidebar's session list by each session's linked pull request, as the session
// list reports it. Kept apart from the owner slice (All, Mine, Shared, Archived), which it
// further narrows.

import type { Conversation } from "@/hooks/useConversations";

/** Which sessions to keep by their pull request. */
export type SessionPullRequestFilter = "all" | "open" | "draft" | "closed" | "none";

/** The menu's order. */
export const SESSION_PULL_REQUEST_FILTERS: readonly SessionPullRequestFilter[] = [
  "all",
  "open",
  "draft",
  "closed",
  "none",
];

export const DEFAULT_SESSION_PULL_REQUEST_FILTER: SessionPullRequestFilter = "all";

/** Each choice as the menu names it, in the owner filter's "… sessions" wording. */
export const SESSION_PULL_REQUEST_FILTER_LABELS: Record<SessionPullRequestFilter, string> = {
  all: "All sessions",
  open: "With an open pull request",
  draft: "With a draft pull request",
  closed: "With a merged or closed pull request",
  none: "Without a pull request",
};

type PullRequestFields = Pick<Conversation, "pull_request" | "pull_requests_synced">;

/**
 * Whether `row` stays under `filter`. Open includes drafts. A pull request the session list
 * names counts by its state, even before the server has imported all of the session's pull
 * requests: a known one is known. A state not fetched yet matches only All, and so does a
 * session with none named until the import says it has none.
 */
export function matchesSessionPullRequestFilter(
  row: PullRequestFields,
  filter: SessionPullRequestFilter,
): boolean {
  if (filter === "all") return true;
  const pullRequest = row.pull_request;
  if (!pullRequest) return filter === "none" && row.pull_requests_synced === true;
  const state = pullRequest.state?.toUpperCase();
  switch (filter) {
    case "open":
      return state === "OPEN";
    case "draft":
      return state === "OPEN" && pullRequest.is_draft === true;
    case "closed":
      return state === "MERGED" || state === "CLOSED";
    case "none":
      return false;
  }
}

/**
 * The rows that stay under `filter`, and the open session's row whatever its pull request, so
 * the session in view stays listed. The same array when the filter keeps all of them.
 */
export function applySessionPullRequestFilter<Row extends PullRequestFields & { id: string }>(
  rows: Row[],
  filter: SessionPullRequestFilter,
  openSessionId: string | null = null,
): Row[] {
  if (filter === "all") return rows;
  return rows.filter(
    (row) => row.id === openSessionId || matchesSessionPullRequestFilter(row, filter),
  );
}
