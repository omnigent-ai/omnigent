import { describe, expect, it } from "vitest";

import type { Conversation } from "@/hooks/useConversations";
import type { SessionPullRequestSummary } from "@/hooks/useSessionPullRequests";

import {
  applySessionPullRequestFilter,
  matchesSessionPullRequestFilter,
  SESSION_PULL_REQUEST_FILTERS,
  type SessionPullRequestFilter,
} from "./sessionListFilters";

const pullRequest = (state: string | null, isDraft = false): SessionPullRequestSummary => ({
  url: "https://example.com/org/repo/pull/1",
  provider: "github",
  repository: "org/repo",
  number: 1,
  state,
  is_draft: isDraft,
});

type Row = Pick<Conversation, "id" | "pull_request" | "pull_requests_synced">;

// An older server sends neither field.
const ROWS: Record<string, Row> = {
  open: { id: "open", pull_request: pullRequest("OPEN"), pull_requests_synced: true },
  draft: { id: "draft", pull_request: pullRequest("OPEN", true), pull_requests_synced: true },
  merged: { id: "merged", pull_request: pullRequest("MERGED"), pull_requests_synced: true },
  closed: { id: "closed", pull_request: pullRequest("CLOSED"), pull_requests_synced: true },
  unfetched: { id: "unfetched", pull_request: pullRequest(null), pull_requests_synced: true },
  none: { id: "none", pull_request: null, pull_requests_synced: true },
  notImported: { id: "notImported", pull_request: null, pull_requests_synced: false },
  // A pull request linked to the session before the server imported the rest of them.
  knownNotImported: {
    id: "knownNotImported",
    pull_request: pullRequest("OPEN"),
    pull_requests_synced: false,
  },
  olderServer: { id: "olderServer" },
};

const kept = (filter: SessionPullRequestFilter) =>
  Object.entries(ROWS)
    .filter(([, row]) => matchesSessionPullRequestFilter(row, filter))
    .map(([name]) => name);

describe("matchesSessionPullRequestFilter", () => {
  it("keeps every session under All, whatever is known of its pull request", () => {
    expect(kept("all")).toEqual(Object.keys(ROWS));
  });

  it("keeps open pull requests, drafts included, under Open", () => {
    expect(kept("open")).toEqual(["open", "draft", "knownNotImported"]);
  });

  it("keeps only open drafts under Draft", () => {
    expect(kept("draft")).toEqual(["draft"]);
  });

  it("keeps merged and closed pull requests under Merged or closed", () => {
    expect(kept("closed")).toEqual(["merged", "closed"]);
  });

  it("keeps only sessions known to have none under Without", () => {
    // Before the server imports a session's pull requests, a missing one isn't known to be none.
    expect(kept("none")).toEqual(["none"]);
  });

  it("reads the state in any case", () => {
    expect(matchesSessionPullRequestFilter({ pull_request: pullRequest("open") }, "open")).toBe(
      true,
    );
  });

  it("drops a session whose state isn't fetched yet from every choice but All", () => {
    for (const filter of SESSION_PULL_REQUEST_FILTERS) {
      expect(matchesSessionPullRequestFilter(ROWS.unfetched, filter)).toBe(filter === "all");
    }
  });
});

describe("applySessionPullRequestFilter", () => {
  it("returns the same rows under All, and the kept rows in order otherwise", () => {
    const rows = [ROWS.merged, ROWS.open, ROWS.none, ROWS.draft];
    expect(applySessionPullRequestFilter(rows, "all")).toBe(rows);
    expect(applySessionPullRequestFilter(rows, "open")).toEqual([ROWS.open, ROWS.draft]);
  });

  it("keeps the open session listed though its pull request doesn't match", () => {
    const rows = [ROWS.merged, ROWS.open, ROWS.none];
    expect(applySessionPullRequestFilter(rows, "open", "none")).toEqual([ROWS.open, ROWS.none]);
  });
});
