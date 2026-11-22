import { describe, expect, it } from "vitest";

import type { Conversation } from "@/hooks/useConversations";

import { isPullRequestLookup, matchSessions } from "./sessionMatching";

function session(id: string, overrides: Partial<Conversation> = {}): Conversation {
  return {
    id,
    object: "conversation",
    title: `Session ${id}`,
    created_at: 0,
    updated_at: 0,
    labels: {},
    permission_level: null,
    ...overrides,
  };
}

function withPullRequest(id: string, number: number, overrides: Partial<Conversation> = {}) {
  return session(id, {
    pull_request: {
      url: `https://github.com/acme/app/pull/${number}`,
      provider: "github",
      repository: "acme/app",
      number,
    },
    ...overrides,
  });
}

const ids = (matches: ReturnType<typeof matchSessions>) => matches.map((match) => match.session.id);

describe("matchSessions", () => {
  it("fuzzy-matches titles and ranks better matches first", () => {
    const sessions = [
      session("s1", { title: "Fix the parser" }),
      session("s2", { title: "Parser rewrite" }),
      session("s3", { title: "Deploy API" }),
    ];
    expect(ids(matchSessions(sessions, "parser"))).toEqual(["s2", "s1"]);
    expect(ids(matchSessions(sessions, "fxprs"))).toEqual(["s1"]);
    expect(matchSessions(sessions, "zzz")).toEqual([]);
  });

  it("matches nothing for an empty query", () => {
    expect(matchSessions([session("a")], "   ")).toEqual([]);
  });

  it("requires every word to match, and says where the label matched", () => {
    const sessions = [session("both", { title: "Fix login" }), session("one", { title: "Login" })];
    const [match, ...rest] = matchSessions(sessions, "fix login");
    expect(rest).toEqual([]);
    expect(match.session.id).toBe("both");
    expect(match.labelRanges.map((range) => "Fix login".slice(range.start, range.end))).toEqual([
      "Fix",
      "login",
    ]);
  });

  it("puts the most recently active first among equally good matches", () => {
    const sessions = [
      session("old", { title: "Deploy", updated_at: 1 }),
      session("new", { title: "Deploy", updated_at: 5 }),
    ];
    expect(ids(matchSessions(sessions, "deploy"))).toEqual(["new", "old"]);
  });

  it("finds sessions by branch, agent and session ID", () => {
    const sessions = [
      session("abc123def", { git_branch: "feature/retry-uploads", agent_name: "researcher" }),
      session("other"),
    ];
    expect(ids(matchSessions(sessions, "retry-uploads"))).toEqual(["abc123def"]);
    expect(ids(matchSessions(sessions, "researcher"))).toEqual(["abc123def"]);
    expect(ids(matchSessions(sessions, "abc123"))).toEqual(["abc123def"]);
  });

  it("treats #123 as a pull request lookup, not a fuzzy search", () => {
    const sessions = [
      withPullRequest("linked", 12101),
      withPullRequest("other-pr", 99),
      // The digits are scattered through this ID and title, which must not match.
      session("s1x2x1x0x1", { title: "Ticket 1 2 1 0 1" }),
    ];
    expect(ids(matchSessions(sessions, "#12101"))).toEqual(["linked"]);
    expect(ids(matchSessions(sessions, "acme/app#12101"))).toEqual(["linked"]);
  });

  it("finds the session linked to a pasted pull request link, including its sub-pages", () => {
    const sessions = [withPullRequest("linked", 7), withPullRequest("other", 70)];
    expect(ids(matchSessions(sessions, "https://github.com/acme/app/pull/7"))).toEqual(["linked"]);
    expect(ids(matchSessions(sessions, "https://GitHub.com/acme/app/pull/7/files?w=1"))).toEqual([
      "linked",
    ]);
    expect(matchSessions(sessions, "https://github.com/acme/app/pull/8")).toEqual([]);
  });

  it.each([
    "https://github.com/ACME/App/pull/42",
    "https://GITHUB.com/acme/APP/pull/42/files",
    "https://GITHUB.COM/ACME/APP/PULL/42",
    "https://GitHub.com/Acme/App/PuLl/42/FiLeS",
    "HTTPS://github.com/acme/app/pull/42/",
  ])("finds a pull request however its link is cased: %s", (link) => {
    const sessions = [withPullRequest("linked", 42), withPullRequest("longer", 420)];
    expect(isPullRequestLookup(link)).toBe(true);
    expect(ids(matchSessions(sessions, link))).toEqual(["linked"]);
  });

  it("keeps a pull request's number whole, in any case", () => {
    const sessions = [withPullRequest("linked", 42)];
    expect(matchSessions(sessions, "https://github.com/acme/app/PULL/420/files")).toEqual([]);
    expect(matchSessions(sessions, "https://github.com/acme/app/pull/4")).toEqual([]);
    // Not a pull request page, so it's text to search for.
    expect(isPullRequestLookup("https://github.com/acme/app/PULL/42x/files")).toBe(false);
    expect(isPullRequestLookup("https://github.com/acme/app/PULLS")).toBe(false);
  });

  it("knows a pull request reference when it sees one", () => {
    expect(isPullRequestLookup(" #8 ")).toBe(true);
    expect(isPullRequestLookup("https://github.com/acme/app/pull/8")).toBe(true);
    expect(isPullRequestLookup("https://gitlab.example.com/acme/app/-/merge_requests/8")).toBe(
      true,
    );
    expect(isPullRequestLookup("https://GitLab.example.com/Acme/App/-/Merge_Requests/8")).toBe(
      true,
    );
    expect(isPullRequestLookup("pr 8")).toBe(false);
    expect(isPullRequestLookup("#8 notes")).toBe(false);
    // Any other link is text to search for.
    expect(isPullRequestLookup("https://example.test/design")).toBe(false);
    expect(isPullRequestLookup("https://github.com/acme/app/issues/8")).toBe(false);
  });
});
