import { describe, expect, it } from "vitest";

import type { Conversation } from "@/hooks/useConversations";
import type { SessionPullRequestSummary } from "@/hooks/useSessionPullRequests";

import {
  applySessionListFilter,
  applySessionPullRequestFilter,
  DEFAULT_SESSION_LIST_FILTER,
  matchesSessionPullRequestFilter,
  parseSessionListFilter,
  SESSION_PULL_REQUEST_FILTERS,
  SESSION_STATUS_FILTERS,
  type SessionListFilter,
  type SessionListFilterContext,
  type SessionPullRequestFilter,
  sessionHarnessChoices,
  sessionHarnessKey,
  sessionHarnessName,
  sessionListFilterCount,
  sessionProjectId,
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

describe("the sidebar's filters together", () => {
  /** A session with an open pull request, its status, harness key and project given by `facts`. */
  function session(id: string, facts: { status: string; harness: string; project: string | null }) {
    return {
      row: { id, pull_request: pullRequest("OPEN"), pull_requests_synced: true } as Conversation,
      ...facts,
    };
  }
  const SESSIONS = [
    session("working-claude-alpha", {
      status: "Working",
      harness: "native:claude",
      project: "alpha",
    }),
    session("done-claude-unfiled", { status: "Done", harness: "native:claude", project: null }),
    session("working-codex-beta", { status: "Working", harness: "codex", project: "beta" }),
  ];
  const facts = (row: Conversation) => SESSIONS.find((entry) => entry.row.id === row.id)!;
  const context: SessionListFilterContext = {
    statusOf: (row) => facts(row).status,
    harnessOf: (row) => facts(row).harness,
    projectOf: (row) => facts(row).project,
  };
  const rows = SESSIONS.map((entry) => entry.row);
  const keptTogether = (filter: Partial<SessionListFilter>, openSessionId: string | null = null) =>
    applySessionListFilter(
      rows,
      { ...DEFAULT_SESSION_LIST_FILTER, ...filter },
      context,
      openSessionId,
    ).map((row) => row.id);

  it("keeps every row, as the same array, with no filter on", () => {
    expect(applySessionListFilter(rows, DEFAULT_SESSION_LIST_FILTER, context)).toBe(rows);
    expect(sessionListFilterCount(DEFAULT_SESSION_LIST_FILTER)).toBe(0);
  });

  it("keeps the sessions in the chosen status, harness or project", () => {
    expect(keptTogether({ status: "working" })).toEqual([
      "working-claude-alpha",
      "working-codex-beta",
    ]);
    expect(keptTogether({ status: "done" })).toEqual(["done-claude-unfiled"]);
    expect(keptTogether({ status: "attention" })).toEqual([]);
    expect(keptTogether({ harness: "native:claude" })).toEqual([
      "working-claude-alpha",
      "done-claude-unfiled",
    ]);
    expect(keptTogether({ project: { kind: "project", id: "beta" } })).toEqual([
      "working-codex-beta",
    ]);
    expect(keptTogether({ project: { kind: "none" } })).toEqual(["done-claude-unfiled"]);
  });

  it("keeps only the sessions every filter keeps, and counts each filter on", () => {
    const filter: SessionListFilter = {
      ...DEFAULT_SESSION_LIST_FILTER,
      status: "working",
      harness: "native:claude",
    };
    expect(keptTogether(filter)).toEqual(["working-claude-alpha"]);
    expect(sessionListFilterCount(filter)).toBe(2);
    expect(keptTogether({ ...filter, pullRequest: "closed" })).toEqual([]);
    expect(
      sessionListFilterCount({ ...filter, pullRequest: "open", project: { kind: "none" } }),
    ).toBe(4);
  });

  it("keeps the open session listed though no filter keeps it", () => {
    expect(keptTogether({ status: "done" }, "working-codex-beta")).toEqual([
      "done-claude-unfiled",
      "working-codex-beta",
    ]);
  });

  it("reads each filter on its own, falling back to All", () => {
    expect(parseSessionListFilter(null)).toEqual(DEFAULT_SESSION_LIST_FILTER);
    expect(
      parseSessionListFilter({
        pullRequest: "open",
        status: "sleeping",
        harness: 3,
        project: { kind: "project" },
      }),
    ).toEqual({ ...DEFAULT_SESSION_LIST_FILTER, pullRequest: "open" });
    // Sessions without a harness are a choice of their own, not All.
    expect(parseSessionListFilter({ harness: "" }).harness).toBe("");
    const every: SessionListFilter = {
      pullRequest: "draft",
      status: "unread",
      harness: "codex",
      project: { kind: "project", id: "alpha" },
    };
    expect(parseSessionListFilter(JSON.parse(JSON.stringify(every)))).toEqual(every);
    expect(parseSessionListFilter({ project: { kind: "none" } }).project).toEqual({ kind: "none" });
    expect(
      SESSION_STATUS_FILTERS.every(
        (status) => parseSessionListFilter({ status }).status === status,
      ),
    ).toBe(true);
  });
});

describe("sessionHarnessKey and its choices", () => {
  const LABELS = { codex: "Codex SDK", "claude-sdk": "Claude SDK" };

  it("keys a native coding agent by the agent, and any other session by its harness", () => {
    expect(sessionHarnessKey({ harness: "claude-native" })).toBe("native:claude");
    expect(sessionHarnessKey({ harness: "codex" })).toBe("codex");
    expect(sessionHarnessKey({ harness: null })).toBe("");
    expect(sessionHarnessName("native:claude", LABELS)).toBe("Claude Code");
    expect(sessionHarnessName("codex", LABELS)).toBe("Codex SDK");
    expect(sessionHarnessName("acp:custom", LABELS)).toBe("acp:custom");
    expect(sessionHarnessName("", LABELS)).toBe("Unknown harness");
  });

  it("offers the loaded sessions' harnesses once each, by name, and the chosen one though none uses it", () => {
    const rows = [{ harness: "codex" }, { harness: "claude-native" }, { harness: "codex" }];
    expect(sessionHarnessChoices(rows, null, LABELS)).toEqual([
      { key: "native:claude", name: "Claude Code" },
      { key: "codex", name: "Codex SDK" },
    ]);
    expect(sessionHarnessChoices(rows, "claude-sdk", LABELS).map((choice) => choice.key)).toEqual([
      "native:claude",
      "claude-sdk",
      "codex",
    ]);
  });
});

describe("sessionProjectId", () => {
  const PROJECTS = [
    { id: "p-alpha", name: "Alpha" },
    { id: "p-beta", name: "Beta" },
  ];

  it("reads the project id first, then a legacy label naming a project", () => {
    expect(
      sessionProjectId({ project_id: "p-beta", labels: { omni_project: "Alpha" } }, PROJECTS),
    ).toBe("p-beta");
    expect(sessionProjectId({ labels: { omni_project: "Alpha" } }, PROJECTS)).toBe("p-alpha");
  });

  it("is none without either, or with a label naming no project", () => {
    expect(sessionProjectId({}, PROJECTS)).toBeNull();
    expect(
      sessionProjectId({ project_id: null, labels: { omni_project: "Gone" } }, PROJECTS),
    ).toBeNull();
  });
});
