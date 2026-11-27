import { describe, expect, it } from "vitest";

import { planOpen, type OpenState } from "../planOpen";
import {
  allPullRequestsChosen,
  changesComparisonShown,
  changesFileInNewTab,
  changesOverview,
  fileTabFileChosen,
  changedFileViewed,
  changesDefaultComparison,
  fileViewDiffRequested,
  pullRequestChosen,
  pullRequestSectionChosen,
  shownPullRequestPlace,
  treeFileChosen,
  unshownPullRequestParts,
} from "./placeViewActions";

const PR = "https://github.com/o/r/pull/12";

describe("Files tab", () => {
  it("shows a tree click in the Files tab itself, and a ⌥/middle click in a new tab", () => {
    expect(treeFileChosen("src/a.ts")).toEqual({
      type: "open",
      link: { place: { kind: "files", path: "src/a.ts" } },
      options: { opener: "user", origin: "files-tree" },
    });
    expect(treeFileChosen("src/a.ts", { newTab: true })).toEqual({
      type: "open",
      link: { place: { kind: "files", path: "src/a.ts" } },
      options: { opener: "user", disposition: "new" },
    });
  });

  it("lands a Files-tab tree click in the Files tab, and a ⌘-click in a new tab, beside another file tab", () => {
    const state: OpenState = {
      sessionId: "s1",
      tabs: [
        { id: "F", place: { kind: "files", path: "a.ts" }, role: "files", dirty: false },
        { id: "A", place: { kind: "files", path: "x.ts" }, dirty: false },
      ],
      selectedTabId: "F",
      recentlySelected: ["F", "A"],
      resourceState: () => "live",
      maxTabs: 200,
    };
    const click = treeFileChosen("b.ts");
    const commandClick = treeFileChosen("b.ts", { newTab: true });
    if (click.type !== "open" || commandClick.type !== "open") throw new Error("expected opens");
    expect(planOpen(state, click.link, click.options)).toMatchObject({
      ok: true,
      plan: { action: "navigate", tabId: "F", place: { kind: "files", path: "b.ts" } },
    });
    expect(planOpen(state, commandClick.link, commandClick.options)).toMatchObject({
      ok: true,
      plan: { action: "create", place: { kind: "files", path: "b.ts" }, after: "F" },
    });
  });

  it("opens the review at this file for Show changes, and opens another file the review names", () => {
    const current = { kind: "files", path: "a.ts" } as const;
    expect(fileViewDiffRequested(current, true, "a.ts")).toEqual({
      type: "open",
      link: { place: { kind: "changes", path: "a.ts" } },
      options: { opener: "user" },
    });
    expect(fileViewDiffRequested(current, false, "a.ts")).toEqual({ type: "none" });
    // A citation still pending goes along to the review; another file's diff cites none.
    const pending = { line: 150, endLine: 151, column: 9 };
    expect(fileViewDiffRequested(current, true, "a.ts", pending)).toMatchObject({
      link: { place: { kind: "changes", path: "a.ts", line: { start: 150, end: 151, column: 9 } } },
    });
    expect(fileViewDiffRequested(current, true, "b.ts", pending)).not.toHaveProperty(
      "link.place.line",
    );
    // The place's own lines alone, a citation already shown, don't go along.
    expect(
      fileViewDiffRequested({ ...current, line: { start: 3 } }, true, "a.ts"),
    ).not.toHaveProperty("link.place.line");
    expect(fileViewDiffRequested(current, false, "b.ts")).toMatchObject({
      type: "open",
      link: { place: { kind: "files", path: "b.ts" } },
    });
  });
});

describe("Changes tab", () => {
  it("goes back to the overview from a file, keeping the visit's comparison", () => {
    expect(changesOverview({ kind: "changes", path: "a.go" })).toEqual({
      type: "navigate",
      place: { kind: "changes" },
      replace: false,
    });
    expect(changesOverview({ kind: "changes" })).toEqual({ type: "none" });
    const comparison = { kind: "saved", intent: "branch-point" } as const;
    expect(changesOverview({ kind: "changes", path: "a.go", comparison })).toMatchObject({
      place: { kind: "changes", comparison },
    });
  });

  it("records a comparison the person picked as a step, and any other in place", () => {
    const shown = { kind: "typed", base: "v1", head: "" } as const;
    const current = { kind: "changes", path: "a.go" } as const;
    expect(changesComparisonShown(current, shown, { step: true, root: null })).toEqual({
      type: "navigate",
      place: { kind: "changes", path: "a.go", comparison: shown },
      replace: false,
    });
    expect(changesComparisonShown(current, shown, { step: false, root: null })).toMatchObject({
      replace: true,
    });
  });

  it("keeps the folder whose review showed the comparison, and drops it for the workspace", () => {
    const shown = { kind: "saved", intent: "head" } as const;
    const worktree = "/repo/.worktrees/feature";
    const inFolder = changesComparisonShown({ kind: "changes" }, shown, {
      step: true,
      root: worktree,
    });
    expect(inFolder).toMatchObject({
      place: { kind: "changes", comparison: shown, comparisonRoot: worktree },
    });
    const backHome = changesComparisonShown(
      { kind: "changes", comparison: shown, comparisonRoot: worktree },
      shown,
      { step: true, root: null },
    );
    expect(backHome).toEqual({
      type: "navigate",
      place: { kind: "changes", comparison: shown },
      replace: false,
    });
    // A file's own tab and the overview keep the visit's folder with its comparison.
    const visit = {
      kind: "changes" as const,
      path: "a.go",
      comparison: shown,
      comparisonRoot: worktree,
    };
    expect(changesOverview({ ...visit })).toMatchObject({
      place: { comparison: shown, comparisonRoot: worktree },
    });
    expect(changesFileInNewTab({ ...visit }, "b.go", { background: false })).toMatchObject({
      link: { place: { path: "b.go", comparison: shown, comparisonRoot: worktree } },
    });
  });
});

describe("pull request tab", () => {
  const page = { kind: "pull", url: PR, view: "summary" } as const;

  it("steps the pull requests tab to a chosen pull request, canonical and on its section", () => {
    expect(pullRequestChosen(`${PR}/files`)).toEqual({
      type: "navigate",
      place: { kind: "pull", url: PR, view: "changes" },
      replace: false,
    });
    expect(pullRequestChosen(PR, "comments")).toMatchObject({
      place: { kind: "pull", url: PR, view: "comments" },
    });
    expect(pullRequestChosen(PR)).toMatchObject({ place: { view: "summary" } });
  });

  it("records a section picked on the page as a step in its tab", () => {
    expect(pullRequestSectionChosen(page, "comments")).toEqual({
      type: "navigate",
      place: { kind: "pull", url: PR, view: "comments" },
      replace: false,
    });
    expect(pullRequestSectionChosen(page, "summary")).toEqual({ type: "none" });
  });

  it("shows the session's list of pull requests in its own tab, not this one", () => {
    expect(allPullRequestsChosen()).toEqual({
      type: "open",
      link: { place: { kind: "pulls" } },
      options: { opener: "user", disposition: "focus" },
    });
  });
});

describe("pull request parts the tab can't show yet", () => {
  it.each([
    [
      { kind: "pull", url: PR, view: "summary", path: "a.ts", line: { start: 4 } },
      "This link points at a.ts line 4, which this tab can't open yet. It shows the pull request's summary instead.",
    ],
    [
      { kind: "pull", url: PR, view: "comments", line: { start: 9 } },
      "This link points at line 9, which this tab can't open yet. It shows the pull request's comments instead.",
    ],
    [
      { kind: "pull", url: PR, view: "changes", path: "a.ts", commit: "abcdef123" },
      "This link points at a.ts and commit abcdef1, which this tab can't open yet. It shows all of the pull request's changes instead.",
    ],
    [
      { kind: "pull", url: PR, view: "commits", commit: "abcdef123" },
      "This link points at commit abcdef1, which this tab can't open yet. It shows all of the pull request's changes instead.",
    ],
    [
      { kind: "pull", url: PR, view: "summary", focus: "checks" },
      "This link points at the checks, which this tab can't open yet. It shows the pull request's summary instead.",
    ],
  ] as const)("names everything in %o", (place, text) => {
    expect(unshownPullRequestParts(place)).toBe(text);
    expect(shownPullRequestPlace(place)).toEqual({ kind: "pull", url: PR, view: place.view });
  });

  it("has nothing to say about a section or the list, which the tab shows", () => {
    expect(unshownPullRequestParts({ kind: "pulls" })).toBeNull();
    expect(shownPullRequestPlace({ kind: "pulls" })).toEqual({ kind: "pulls" });
    expect(unshownPullRequestParts({ kind: "pull", url: PR, view: "changes" })).toBeNull();
  });
});

describe("Changes review's links out", () => {
  it("views a changed file in a Files tab", () => {
    expect(changedFileViewed("src/a.ts")).toEqual({
      type: "open",
      link: { place: { kind: "files", path: "src/a.ts" } },
      options: { opener: "user" },
    });
  });

  it("leaves a linked comparison for the session's own, keeping the file", () => {
    expect(
      changesDefaultComparison({ kind: "changes", path: "a.go", range: { base: "main" } }),
    ).toEqual({ type: "navigate", place: { kind: "changes", path: "a.go" }, replace: false });
    expect(changesDefaultComparison({ kind: "changes", path: "a.go" })).toEqual({ type: "none" });
  });
});

describe("changesFileInNewTab", () => {
  it("opens a Diffs tab named for the file, behind this one unless Shift brings it forward", () => {
    const current = { kind: "changes" as const, path: "a.go", range: { base: "main" } };
    expect(changesFileInNewTab(current, "b.go", { background: true })).toEqual({
      type: "open",
      link: { place: { kind: "changes", path: "b.go", fileTab: true, range: { base: "main" } } },
      options: { opener: "user", disposition: "focus", reuse: "same-place", keepSelection: true },
    });
    expect(changesFileInNewTab({ kind: "changes" }, "b.go", { background: false })).toEqual({
      type: "open",
      link: { place: { kind: "changes", path: "b.go", fileTab: true } },
      options: { opener: "user", disposition: "focus", reuse: "same-place", keepSelection: false },
    });
  });

  it("gives the file the comparison picked over a linked range, not the range", () => {
    const lastCommit = { kind: "saved", intent: "head" } as const;
    const picked = changesComparisonShown(
      { kind: "changes", range: { base: "v1.2" } },
      lastCommit,
      { step: true, root: null },
    );
    expect(picked).toEqual({
      type: "navigate",
      place: { kind: "changes", comparison: lastCommit },
      replace: false,
    });
    const place = { kind: "changes", comparison: lastCommit } as const;
    expect(changesFileInNewTab(place, "b.go", { background: true })).toMatchObject({
      link: { place: { kind: "changes", path: "b.go", fileTab: true, comparison: lastCommit } },
    });
  });
});

describe("fileTabFileChosen", () => {
  it("steps the file tab to a file picked from its breadcrumbs, and ignores its own file", () => {
    expect(fileTabFileChosen({ kind: "files", path: "a.ts" }, "src/b.ts")).toEqual({
      type: "navigate",
      place: { kind: "files", path: "src/b.ts" },
      replace: false,
    });
    expect(fileTabFileChosen({ kind: "files", path: "a.ts" }, "a.ts")).toEqual({ type: "none" });
  });
});
