import { parsePatchFiles } from "@pierre/diffs";
import { describe, expect, it } from "vitest";

import {
  commitAuthor,
  commitChangedFiles,
  commitCiLabel,
  commitCiTone,
  commitTime,
  commitListRevision,
  commitStepDirection,
  commitsToReadAhead,
  type CommitsTabFacts,
  commitsTabStatus,
  isLongCommitBody,
  openCommitMembership,
  type PullRequestCommit,
  type PullRequestCommits,
  shortSha,
} from "./pullRequestCommits";

const COMMIT: PullRequestCommit = {
  sha: "1a6658a495de30eaa4d56bec3af8406923855897",
  subject: "Bundle gh",
  author_login: "octo_data",
  author_name: "Octo Cat",
  authored_at: "2026-11-03T10:07:37Z",
  committed_at: "2026-11-03T10:17:50Z",
  url: null,
  ci_state: "SUCCESS",
};

describe("commitCiTone", () => {
  it.each([
    ["SUCCESS", "passing"],
    ["FAILURE", "failing"],
    ["ERROR", "failing"],
    ["PENDING", "pending"],
    ["EXPECTED", "pending"],
    [null, "none"],
    ["SOMETHING_NEW", "none"],
  ] as const)("maps %s to %s", (state, tone) => {
    expect(commitCiTone(state)).toBe(tone);
  });

  it("names each tone for assistive tech", () => {
    expect(commitCiLabel("failing")).toBe("Checks failed");
    expect(commitCiLabel("none")).toBe("No checks");
  });
});

describe("commit details", () => {
  it("shortens the SHA to GitHub's seven characters", () => {
    expect(shortSha(COMMIT.sha)).toBe("1a6658a");
  });

  it("credits the login, then the git author name", () => {
    expect(commitAuthor(COMMIT)).toBe("octo_data");
    expect(commitAuthor({ ...COMMIT, author_login: null })).toBe("Octo Cat");
    expect(commitAuthor({ ...COMMIT, author_login: null, author_name: null })).toBeNull();
  });

  it("dates a commit by its author time, then its commit time", () => {
    expect(commitTime(COMMIT)).toBe(Date.parse("2026-11-03T10:07:37Z"));
    expect(commitTime({ ...COMMIT, authored_at: null })).toBe(Date.parse("2026-11-03T10:17:50Z"));
    expect(commitTime({ ...COMMIT, authored_at: null, committed_at: null })).toBeNull();
  });
});

describe("commitChangedFiles", () => {
  it("reads each file's status and line counts from the patch", () => {
    const patch = [
      "diff --git a/src/app.ts b/src/app.ts",
      "index 1111111..2222222 100644",
      "--- a/src/app.ts",
      "+++ b/src/app.ts",
      "@@ -1,2 +1,3 @@",
      " keep",
      "-old",
      "+new",
      "+added",
      "diff --git a/docs/new.md b/docs/new.md",
      "new file mode 100644",
      "index 0000000..3333333",
      "--- /dev/null",
      "+++ b/docs/new.md",
      "@@ -0,0 +1 @@",
      "+hello",
      "diff --git a/gone.txt b/gone.txt",
      "deleted file mode 100644",
      "index 4444444..0000000",
      "--- a/gone.txt",
      "+++ /dev/null",
      "@@ -1 +0,0 @@",
      "-bye",
      "diff --git a/old/name.ts b/new/name.ts",
      "similarity index 100%",
      "rename from old/name.ts",
      "rename to new/name.ts",
      "",
    ].join("\n");
    const files = commitChangedFiles(parsePatchFiles(patch).flatMap((parsed) => parsed.files));
    expect(
      files.map(({ path, name, status, lines_added, lines_removed }) => ({
        path,
        name,
        status,
        lines_added,
        lines_removed,
      })),
    ).toEqual([
      { path: "src/app.ts", name: "app.ts", status: "modified", lines_added: 2, lines_removed: 1 },
      { path: "docs/new.md", name: "new.md", status: "created", lines_added: 1, lines_removed: 0 },
      { path: "gone.txt", name: "gone.txt", status: "deleted", lines_added: 0, lines_removed: 1 },
      { path: "new/name.ts", name: "name.ts", status: "renamed", lines_added: 0, lines_removed: 0 },
    ]);
  });
});

describe("commitsTabStatus", () => {
  const facts = (overrides: Partial<CommitsTabFacts> = {}): CommitsTabFacts => ({
    path: "host",
    fastTrack: { status: "unsupported" },
    list: { hasData: false, error: null },
    timedOut: false,
    onTab: true,
    ...overrides,
  });

  it("never waits on the fast track for a PR read through the host", () => {
    expect(commitsTabStatus(facts())).toEqual({ kind: "loading" });
    expect(commitsTabStatus(facts({ list: { hasData: true, error: null } }))).toEqual({
      kind: "ready",
    });
  });

  it("hides the tab where nothing can read the PR's commits", () => {
    expect(commitsTabStatus(facts({ path: null }))).toEqual({ kind: "hidden" });
    expect(commitsTabStatus(facts({ path: "fast-track" }))).toEqual({ kind: "hidden" });
  });

  it("says why the fast track can't read, instead of spinning", () => {
    const status = commitsTabStatus(
      facts({
        path: "fast-track",
        fastTrack: { status: "unavailable", message: "Run gh auth login." },
      }),
    );
    expect(status).toEqual({ kind: "unavailable", message: "Run gh auth login." });
  });

  it("shows a fast-track check only on the tab, and says when it takes too long", () => {
    const checking = facts({ path: "fast-track", fastTrack: { status: "checking" } });
    expect(commitsTabStatus({ ...checking, onTab: false })).toEqual({ kind: "hidden" });
    expect(commitsTabStatus(checking)).toEqual({ kind: "checking" });
    expect(commitsTabStatus({ ...checking, timedOut: true })).toEqual({ kind: "slow" });
  });

  it("tells a failed read from a slow one, and keeps a list it already has", () => {
    const error = new Error("Example Host couldn't answer.");
    expect(commitsTabStatus(facts({ list: { hasData: false, error } }))).toEqual({
      kind: "failed",
      message: "Example Host couldn't answer.",
      // The read's own error, so a Mac sign-in problem can offer its recovery.
      error,
    });
    expect(commitsTabStatus(facts({ timedOut: true }))).toEqual({ kind: "slow" });
    expect(commitsTabStatus(facts({ list: { hasData: true, error } }))).toEqual({ kind: "ready" });
  });
});

describe("openCommitMembership", () => {
  const HEAD = "a".repeat(40);
  const BASE = "b".repeat(40);
  const OPEN = "c".repeat(40);
  const commit = (sha: string): PullRequestCommit => ({
    sha,
    subject: "",
    author_login: null,
    author_name: null,
    authored_at: null,
    committed_at: null,
    url: null,
    ci_state: null,
  });
  const list = (overrides: Partial<PullRequestCommits> = {}): PullRequestCommits => ({
    total_count: 1,
    commits: [commit("d".repeat(40))],
    head_sha: HEAD,
    base_sha: BASE,
    complete: true,
    ...overrides,
  });
  const now = { headSha: HEAD, baseSha: BASE };

  it("finds a listed commit, and says one has left only from the whole history now", () => {
    expect(openCommitMembership("d".repeat(40), list(), now)).toBe("listed");
    expect(openCommitMembership(OPEN, list(), now)).toBe("left");
  });

  it("can't tell past GitHub's 250-commit limit", () => {
    expect(openCommitMembership(OPEN, list({ complete: false, total_count: 300 }), now)).toBe(
      "unknown",
    );
  });

  it("can't tell from a list read at an older head, or before the base moved", () => {
    expect(openCommitMembership(OPEN, list({ head_sha: "e".repeat(40) }), now)).toBe("unknown");
    // A retarget or a base that advanced, with the head unchanged.
    expect(openCommitMembership(OPEN, list(), { headSha: HEAD, baseSha: "f".repeat(40) })).toBe(
      "unknown",
    );
  });

  it("can't tell from an older desktop app's list without the head, base or completeness", () => {
    for (const older of [
      { head_sha: undefined },
      { base_sha: undefined },
      { complete: undefined },
    ] as Partial<PullRequestCommits>[]) {
      expect(openCommitMembership(OPEN, list(older), now)).toBe("unknown");
    }
  });

  it("names a list's revision against the pull request's", () => {
    expect(commitListRevision(list(), now)).toBe("current");
    expect(commitListRevision(list({ base_sha: "f".repeat(40) }), now)).toBe("other");
    expect(commitListRevision(list({ head_sha: null }), now)).toBe("unknown");
    expect(commitListRevision(list(), { headSha: HEAD, baseSha: undefined })).toBe("unknown");
  });
});

describe("the commits read ahead of the open one", () => {
  const SHAS = ["a", "b", "c", "d", "e"];

  it("reads both neighbours on opening, the next one first", () => {
    expect(commitsToReadAhead(SHAS, 2, null)).toEqual(["d", "b"]);
  });

  it("after a step, leads the way it went and reads one more that way", () => {
    expect(commitsToReadAhead(SHAS, 2, 1)).toEqual(["d", "b", "e"]);
    expect(commitsToReadAhead(SHAS, 2, -1)).toEqual(["b", "d", "a"]);
  });

  it("stops at the ends of the list", () => {
    expect(commitsToReadAhead(SHAS, 0, -1)).toEqual(["b"]);
    expect(commitsToReadAhead(SHAS, 4, 1)).toEqual(["d"]);
    expect(commitsToReadAhead(["a"], 0, null)).toEqual([]);
  });

  it("reads nothing for a commit the list doesn't include", () => {
    expect(commitsToReadAhead(SHAS, -1, null)).toEqual([]);
  });

  it("counts only a move to a neighbour as a step", () => {
    expect(commitStepDirection(SHAS, "b", "c")).toBe(1);
    expect(commitStepDirection(SHAS, "c", "b")).toBe(-1);
    expect(commitStepDirection(SHAS, "a", "d")).toBeNull();
    expect(commitStepDirection(SHAS, undefined, "b")).toBeNull();
    expect(commitStepDirection(SHAS, "b", undefined)).toBeNull();
    expect(commitStepDirection(SHAS, "gone", "a")).toBeNull();
  });
});

describe("isLongCommitBody", () => {
  it("folds a description past six lines, or one long enough to wrap past them", () => {
    expect(isLongCommitBody("one\ntwo\nthree\nfour\nfive\nsix")).toBe(false);
    expect(isLongCommitBody("one\ntwo\nthree\nfour\nfive\nsix\nseven")).toBe(true);
    expect(isLongCommitBody("x".repeat(480))).toBe(false);
    expect(isLongCommitBody("x".repeat(481))).toBe(true);
  });
});
