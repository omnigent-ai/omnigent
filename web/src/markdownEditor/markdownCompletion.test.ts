import { describe, expect, it } from "vitest";

import type { Person } from "@/lib/peopleSearch";

import {
  completionEdit,
  completionText,
  completionTokenAt,
  type PullRequestCandidate,
  pullRequestCompletionQuery,
  rankPullRequests,
} from "./markdownCompletion";

/** The token at the caret, written as `|` in `text`. */
function tokenIn(text: string) {
  const [before = "", ...after] = text.split("|");
  return completionTokenAt(before + after.join("|"), before.length);
}

describe("completionTokenAt", () => {
  it.each([
    ["@|", { kind: "person", query: "" }],
    ["Thanks @oc|", { kind: "person", query: "oc" }],
    ["cc @acme/rev|", { kind: "person", query: "acme/rev" }],
    ["(@oct|", { kind: "person", query: "oct" }],
    ["#|", { kind: "pullRequest", query: "" }],
    ["Fixes #42|", { kind: "pullRequest", query: "42" }],
    ["see #upload|", { kind: "pullRequest", query: "upload" }],
    ["line one\n#4|", { kind: "pullRequest", query: "4" }],
    ["@oc| later", { kind: "person", query: "oc" }],
  ])("completes %s", (text, expected) => {
    expect(tokenIn(text)).toMatchObject(expected);
  });

  it.each([
    ["mail me at amy@exam|"],
    ["a word#4|"],
    ["https://example.com/#anch|"],
    ["`@oc|"],
    ["see `a`, `@oc|"],
    ["`` a ` b @oc|"],
    ["    @oc|"],
    ["```@oc|"],
    ["  ~~~ js @oc|"],
    ["[docs](https://example.com/@oc|"],
    ["@octo|cat"],
    ["@-x|"],
    ["#4/5|"],
    ["plain text|"],
  ])("leaves %s alone", (text) => {
    expect(tokenIn(text)).toBeNull();
  });

  it.each([
    ["after a closed code span", "`code` @oc|"],
    ["on the line after an unclosed backtick", "a ` b\n@oc|"],
    ["after a closed double-backtick span", "`` code `` @oc|"],
    ["with three spaces of indent", "   @oc|"],
    // Only the token's own line is read: a fenced block's body completes, by design.
    ["inside a fenced block's body", "```\n@oc|"],
  ])("completes %s", (_case, text) => {
    expect(tokenIn(text)).toMatchObject({ kind: "person", query: "oc" });
  });

  it("spans the trigger to the caret", () => {
    expect(tokenIn("hi @oc|")).toEqual({ kind: "person", start: 3, end: 6, query: "oc" });
  });
});

const person = (login: string, name: string | null = null): Person => ({
  login,
  name,
  kind: "user",
});
const pullRequest = (number: number, title: string): PullRequestCandidate => ({
  url: `https://github.com/acme/app/pull/${number}`,
  number,
  title,
  state: "OPEN",
  isDraft: false,
});

describe("rankPullRequests", () => {
  it("puts the exact number first, then numbers it starts, then titles holding it", () => {
    const pulls = [
      pullRequest(420, "Speed up 42 things"),
      pullRequest(7, "Fix 42"),
      pullRequest(42, "Ship it"),
    ];
    expect(rankPullRequests(pulls, "42").map((each) => each.number)).toEqual([42, 420, 7]);
  });

  it("matches title words case-insensitively and lists each PR once", () => {
    const pulls = [
      pullRequest(1, "Upload retries"),
      pullRequest(1, "Upload retries"),
      pullRequest(2, "Docs"),
    ];
    expect(rankPullRequests(pulls, "UPLOAD").map((each) => each.number)).toEqual([1]);
  });
});

describe("pullRequestCompletionQuery", () => {
  it("asks the shell for the repository and the typed number or word, which it scopes", () => {
    expect(pullRequestCompletionQuery("acme/app", "42")).toEqual({
      repository: "acme/app",
      text: "42",
    });
    expect(pullRequestCompletionQuery("acme/app", "upload")).toEqual({
      repository: "acme/app",
      text: "upload",
    });
  });

  it("never searches a bare #, a qualifier or an unknown repository", () => {
    expect(pullRequestCompletionQuery("acme/app", "")).toBeNull();
    expect(pullRequestCompletionQuery("acme/app", "  ")).toBeNull();
    expect(pullRequestCompletionQuery("acme/app", "repo:other/app")).toBeNull();
    expect(pullRequestCompletionQuery("not a repo", "42")).toBeNull();
  });
});

describe("completionEdit", () => {
  const edit = (value: string, caret: number) => ({
    value,
    selectionStart: caret,
    selectionEnd: caret,
  });

  it("puts the pick and a space in place of the token, the caret after both", () => {
    const token = completionTokenAt("Thanks @oc", 10)!;
    const text = completionText({ kind: "person", person: person("octo") });
    expect(completionEdit(edit("Thanks @oc", 10), token, text)).toEqual(edit("Thanks @octo ", 13));
  });

  it("keeps the space already after the token, the caret past it", () => {
    const token = completionTokenAt("Fixes #4 now", 8)!;
    const text = completionText({ kind: "pullRequest", pullRequest: pullRequest(42, "Ship") });
    expect(completionEdit(edit("Fixes #4 now", 8), token, text)).toEqual(edit("Fixes #42 now", 10));
  });
});
