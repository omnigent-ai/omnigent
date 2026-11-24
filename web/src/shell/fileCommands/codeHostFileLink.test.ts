import { describe, expect, it } from "vitest";

import {
  codeHostBlobUrl,
  pullRequestFileLink,
  sourceRepository,
  stackComparisonFileLink,
} from "./codeHostFileLink";

const HEAD = "a".repeat(40);

describe("codeHostBlobUrl", () => {
  it("encodes each path segment and keeps the slashes", () => {
    expect(
      codeHostBlobUrl({
        host: "github.com",
        owner: "acme",
        repo: "app",
        sha: HEAD,
        path: "docs/a b/#notes?.md",
      }),
    ).toBe(`https://github.com/acme/app/blob/${HEAD}/docs/a%20b/%23notes%3F.md`);
  });

  it("anchors one line or an ordered range", () => {
    const at = (start: number, end: number) =>
      codeHostBlobUrl({
        host: "github.com",
        owner: "acme",
        repo: "app",
        sha: HEAD,
        path: "a.ts",
        lines: { start, end },
      });
    expect(at(4, 4)).toMatch(/#L4$/);
    expect(at(9, 3)).toMatch(/#L3-L9$/);
  });
});

describe("pullRequestFileLink", () => {
  const facts = {
    prUrl: "https://github.com/acme/app/pull/7",
    headSha: HEAD,
    path: "src/a.ts",
    change: "modified" as const,
  };

  it("opens a github.com file at the PR head", () => {
    expect(pullRequestFileLink(facts)).toEqual({
      label: "Open in GitHub",
      url: `https://github.com/acme/app/blob/${HEAD}/src/a.ts`,
    });
  });

  it("recognizes a GitHub Enterprise host only when it's known to serve GitHub", () => {
    const enterprise = { ...facts, prUrl: "https://git.example.com/acme/app/pull/7" };
    expect(pullRequestFileLink(enterprise)).toBeNull();
    expect(pullRequestFileLink({ ...enterprise, githubHosts: ["git.example.com"] })?.label).toBe(
      "Open in GitHub",
    );
  });

  it("anchors a selection only when it's all on the new side, the one the head shows", () => {
    const newSide = { start: 3, end: 5, side: "additions" as const };
    const oldSide = { start: 3, end: 5, side: "deletions" as const };
    const mixed = { start: 3, end: 5, side: "deletions" as const, endSide: "additions" as const };
    expect(pullRequestFileLink({ ...facts, selection: newSide })?.url).toMatch(/#L3-L5$/);
    expect(pullRequestFileLink({ ...facts, selection: oldSide })?.url).not.toContain("#");
    expect(pullRequestFileLink({ ...facts, selection: mixed })?.url).not.toContain("#");
  });

  it("can't open a deleted file, whose old text is at a merge base the review doesn't know", () => {
    expect(pullRequestFileLink({ ...facts, change: "deleted" })).toEqual({
      label: "Open in GitHub",
      url: null,
      unavailableReason: "Deleted files can't be opened in GitHub.",
    });
  });

  it("offers nothing without a recognized PR link, and says when the commit isn't known", () => {
    expect(pullRequestFileLink({ ...facts, prUrl: null })).toBeNull();
    expect(pullRequestFileLink({ ...facts, prUrl: "https://gitlab.com/a/b/pull/1" })).toBeNull();
    expect(pullRequestFileLink({ ...facts, headSha: undefined })).toMatchObject({
      url: null,
      unavailableReason: "The pull request's latest commit isn't known yet.",
    });
  });

  it("opens a pull request from a fork in the fork, with the path encoded", () => {
    expect(
      pullRequestFileLink({
        ...facts,
        path: "docs/a b.md",
        headRepositoryUrl: "https://github.com/contributor/app-fork",
      })?.url,
    ).toBe(`https://github.com/contributor/app-fork/blob/${HEAD}/docs/a%20b.md`);
  });

  it("falls back to the PR's repository only when the host didn't say, not when it doesn't know", () => {
    expect(pullRequestFileLink(facts)?.url).toBe(
      `https://github.com/acme/app/blob/${HEAD}/src/a.ts`,
    );
    expect(pullRequestFileLink({ ...facts, headRepositoryUrl: null })).toEqual({
      label: "Open in GitHub",
      url: null,
      unavailableReason: "The pull request's source repository isn't known.",
    });
  });
});

describe("sourceRepository", () => {
  it("takes an HTTPS repository on the pull request's own host", () => {
    expect(sourceRepository("https://github.com/fork/app", "github.com")).toEqual({
      owner: "fork",
      repo: "app",
    });
    expect(sourceRepository("https://GitHub.com/fork/app.web/", "github.com")).toEqual({
      owner: "fork",
      repo: "app.web",
    });
    expect(sourceRepository("https://git.example.com/eng/monorepo", "git.example.com")).toEqual({
      owner: "eng",
      repo: "monorepo",
    });
  });

  it("knows it's the PR's own repository when absent, and refuses anything that isn't one", () => {
    expect(sourceRepository(undefined, "github.com")).toBe("same");
    for (const url of [
      null,
      "",
      "not a url",
      "http://github.com/fork/app",
      "https://user:secret@github.com/fork/app",
      "https://github.com:8443/fork/app",
      "https://github.com/fork/app?tab=code",
      "https://github.com/fork/app#readme",
      "https://github.com/fork/app/tree/main",
      "https://github.com/fork",
      "https://github.com/fork/..",
      "https://elsewhere.example.com/fork/app",
    ]) {
      expect(sourceRepository(url, "github.com"), String(url)).toBe("unknown");
    }
  });
});

describe("stackComparisonFileLink", () => {
  const CHILD = "c".repeat(40);
  const PARENT = "b".repeat(40);
  const facts = {
    prUrl: "https://github.com/acme/app/pull/102",
    childHead: CHILD,
    parentHead: PARENT,
    path: "src/new.ts",
    previousPath: "src/old.ts",
    change: "modified" as const,
  };
  const blob = (sha: string, path: string, anchor = "") =>
    `https://github.com/acme/app/blob/${sha}/${path}${anchor}`;

  it("opens new lines at the PR's head, and old lines at its parent's under the old path", () => {
    expect(stackComparisonFileLink(facts)?.url).toBe(blob(CHILD, "src/new.ts"));
    expect(
      stackComparisonFileLink({
        ...facts,
        selection: { start: 3, end: 5, side: "additions", endSide: "additions" },
      })?.url,
    ).toBe(blob(CHILD, "src/new.ts", "#L3-L5"));
    expect(
      stackComparisonFileLink({
        ...facts,
        selection: { start: 7, end: 7, side: "deletions", endSide: "deletions" },
      })?.url,
    ).toBe(blob(PARENT, "src/old.ts", "#L7"));
  });

  it("opens a deleted file at the parent's head, and never an added file's old side", () => {
    expect(stackComparisonFileLink({ ...facts, change: "deleted", previousPath: null })?.url).toBe(
      blob(PARENT, "src/new.ts"),
    );
    expect(
      stackComparisonFileLink({
        ...facts,
        change: "added",
        selection: { start: 1, end: 1, side: "deletions" },
      }),
    ).toBeNull();
    expect(
      stackComparisonFileLink({
        ...facts,
        change: "deleted",
        selection: { start: 1, end: 1, side: "additions" },
      }),
    ).toBeNull();
  });

  it("has no one place to open a selection across both sides", () => {
    expect(
      stackComparisonFileLink({
        ...facts,
        selection: { start: 2, end: 4, side: "deletions", endSide: "additions" },
      }),
    ).toBeNull();
  });
  it("opens each side where its commits live, for a described stack whose heads are forks", () => {
    const forks = {
      ...facts,
      headRepositoryUrl: "https://github.com/child-fork/app",
      parentHeadRepository: "parent-fork/app",
    };
    expect(stackComparisonFileLink(forks)?.url).toBe(
      `https://github.com/child-fork/app/blob/${CHILD}/src/new.ts`,
    );
    expect(
      stackComparisonFileLink({
        ...forks,
        selection: { start: 7, end: 7, side: "deletions", endSide: "deletions" },
      })?.url,
    ).toBe(`https://github.com/parent-fork/app/blob/${PARENT}/src/old.ts#L7`);
    expect(stackComparisonFileLink({ ...forks, change: "deleted", previousPath: null })?.url).toBe(
      `https://github.com/parent-fork/app/blob/${PARENT}/src/new.ts`,
    );
  });

  it("can't open a side whose repository is unknown, and falls back only where nothing was said", () => {
    expect(stackComparisonFileLink({ ...facts, headRepositoryUrl: null })).toEqual({
      label: "Open in GitHub",
      url: null,
      unavailableReason: "The pull request's source repository isn't known.",
    });
    // A deleted parent fork leaves old lines nowhere to open; new ones still open.
    const goneParent = { ...facts, parentHeadRepository: null };
    expect(stackComparisonFileLink({ ...goneParent, change: "deleted" })).toMatchObject({
      url: null,
      unavailableReason: "The parent pull request's source repository isn't known.",
    });
    expect(stackComparisonFileLink(goneParent)?.url).toBe(blob(CHILD, "src/new.ts"));
    expect(
      stackComparisonFileLink({ ...facts, parentHeadRepository: "not/a/repo", change: "deleted" })
        ?.url,
    ).toBeNull();
    // Older data says neither, so both sides open in the pull request's repository.
    expect(stackComparisonFileLink({ ...facts, change: "deleted" })?.url).toBe(
      blob(PARENT, "src/old.ts"),
    );
  });
});
