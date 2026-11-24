import { describe, expect, it } from "vitest";

import { PushUnresolved, RepositoryContextProblem } from "@/lib/workspaceSidebarApi";

import {
  codeHostBlobUrl,
  pullRequestFileLink,
  sourceRepository,
  stackComparisonFileLink,
  type WorkspaceRepositoryContext,
  workspaceFileLink,
} from "./codeHostFileLink";

const HEAD = "a".repeat(40);

describe("codeHostBlobUrl", () => {
  it("encodes each path segment and keeps the slashes", () => {
    expect(
      codeHostBlobUrl({
        host: "github.com",
        owner: "acme",
        repo: "app",
        revision: HEAD,
        path: "docs/a b/#notes?.md",
      }),
    ).toBe(`https://github.com/acme/app/blob/${HEAD}/docs/a%20b/%23notes%3F.md`);
  });

  it("keeps a branch name's slashes and encodes the rest", () => {
    expect(
      codeHostBlobUrl({
        host: "github.com",
        owner: "acme",
        repo: "app",
        revision: "me/fix#2",
        path: "a.ts",
      }),
    ).toBe("https://github.com/acme/app/blob/me/fix%232/a.ts");
  });

  it("anchors one line or an ordered range", () => {
    const at = (start: number, end: number) =>
      codeHostBlobUrl({
        host: "github.com",
        owner: "acme",
        repo: "app",
        revision: HEAD,
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

describe("workspaceFileLink", () => {
  const PUSHED = "c".repeat(40);
  const pushedContext: WorkspaceRepositoryContext = {
    problem: RepositoryContextProblem.UNSPECIFIED,
    branch: "me/feature",
    headSha: PUSHED,
    push: {
      remoteName: "origin",
      branch: "me/feature",
      host: "github.com",
      owner: "acme",
      repo: "app",
      trackingSha: PUSHED,
    },
    pushUnresolved: PushUnresolved.UNSPECIFIED,
    remotes: [{ name: "origin", host: "github.com", owner: "acme", repo: "app" }],
    workspacePrefix: "packages/web/",
  };
  const link = (context: WorkspaceRepositoryContext, path = "src/a.ts", githubHosts?: string[]) =>
    workspaceFileLink({ context, path, githubHosts });
  const notPushed = { ...pushedContext, push: { ...pushedContext.push!, trackingSha: "" } };

  it("opens a pushed HEAD at its commit, below the workspace's place in the repository", () => {
    expect(link(pushedContext)).toEqual({
      label: "Open in GitHub",
      url: `https://github.com/acme/app/blob/${PUSHED}/packages/web/src/a.ts`,
    });
    expect(
      workspaceFileLink({ context: pushedContext, path: "src/a.ts", lines: { start: 9, end: 4 } })
        ?.url,
    ).toBe(`https://github.com/acme/app/blob/${PUSHED}/packages/web/src/a.ts#L4-L9`);
  });

  it("opens the pushed branch, without line anchors, when HEAD has commits the push remote doesn't", () => {
    const ahead = { ...pushedContext, headSha: HEAD };
    const branchUrl = "https://github.com/acme/app/blob/me/feature/packages/web/src/a.ts";
    expect(link(ahead)?.url).toBe(branchUrl);
    // The branch's lines may not be the ones on screen.
    expect(
      workspaceFileLink({ context: ahead, path: "src/a.ts", lines: { start: 4, end: 9 } })?.url,
    ).toBe(branchUrl);
  });

  it("opens the repository the branch pushes to, such as a fork", () => {
    const fork = { ...pushedContext, push: { ...pushedContext.push!, owner: "me" } };
    expect(link(fork)?.url).toBe(`https://github.com/me/app/blob/${PUSHED}/packages/web/src/a.ts`);
  });

  it("says why when the branch isn't pushed yet", () => {
    const pushFirst = {
      label: "Open in GitHub",
      url: null,
      unavailableReason: "Push this branch to open the file in GitHub.",
    };
    expect(link(notPushed)).toEqual(pushFirst);
    expect(link({ ...pushedContext, push: undefined })).toEqual(pushFirst);
    expect(link({ ...pushedContext, branch: "", push: undefined })).toMatchObject({
      url: null,
      unavailableReason: "Check out a branch to open the file in GitHub.",
    });
    expect(
      link({ ...pushedContext, push: undefined, pushUnresolved: PushUnresolved.REFSPEC }),
    ).toMatchObject({
      url: null,
      unavailableReason: "This branch doesn't push to one GitHub repository.",
    });
  });

  it("recognizes a GitHub Enterprise host only from evidence", () => {
    const enterprise = {
      ...pushedContext,
      push: { ...pushedContext.push!, host: "git.example.com" },
      remotes: [{ name: "origin", host: "git.example.com", owner: "acme", repo: "app" }],
    };
    expect(link(enterprise)).toBeNull();
    expect(link({ ...enterprise, push: undefined })).toBeNull();
    expect(link(enterprise, "src/a.ts", ["git.example.com"])?.url).toBe(
      `https://git.example.com/acme/app/blob/${PUSHED}/packages/web/src/a.ts`,
    );
  });

  it("offers nothing without a repository context it can map paths through", () => {
    expect(workspaceFileLink({ context: undefined, path: "src/a.ts" })).toBeNull();
    expect(
      link({ ...pushedContext, problem: RepositoryContextProblem.NOT_A_REPOSITORY }),
    ).toBeNull();
    // An older host doesn't say where the workspace sits in its repository.
    expect(link({ ...pushedContext, workspacePrefix: undefined })).toBeNull();
    expect(link({ ...pushedContext, workspacePrefix: "" })?.url).toBe(
      `https://github.com/acme/app/blob/${PUSHED}/src/a.ts`,
    );
  });

  it.each([
    "/etc/hosts",
    "~/notes.md",
    "C:\\code\\a.ts",
    "../other/a.ts",
    "src/./a.ts",
    "src//a.ts",
  ])("offers nothing for %s, which isn't a path in the workspace", (path) => {
    expect(link(pushedContext, path)).toBeNull();
  });
});
