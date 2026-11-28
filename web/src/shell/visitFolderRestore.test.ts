import { describe, expect, it } from "vitest";

import { landingStands, type VisitFolderInput, visitFolderStep } from "./visitFolderRestore";

const WORKTREE = "/repo/.worktrees/feature";

function step(overrides: Partial<VisitFolderInput> = {}) {
  return visitFolderStep({
    stands: true,
    active: true,
    hasComparison: true,
    visitRoot: WORKTREE,
    shownRoot: null,
    directory: null,
    resolved: true,
    visitRootShows: true,
    canChoose: true,
    ...overrides,
  });
}

describe("landingStands", () => {
  const now = { choices: 2, folders: 3 };

  it("stands while nothing was chosen since it landed", () => {
    expect(landingStands({ settled: false, choices: 2, folders: 3 }, now)).toBe(true);
  });

  it("falls once a comparison or a folder is chosen, or once it settled", () => {
    expect(landingStands({ settled: false, choices: 1, folders: 3 }, now)).toBe(false);
    expect(landingStands({ settled: false, choices: 2, folders: 2 }, now)).toBe(false);
    expect(landingStands({ settled: true, choices: 2, folders: 3 }, now)).toBe(false);
    expect(landingStands(undefined, now)).toBe(false);
  });

  it("ignores folders for a landing stamped without them", () => {
    expect(landingStands({ settled: false, choices: 2 }, now)).toBe(true);
  });
});

describe("visitFolderStep", () => {
  it("restores the visit's worktree when the review shows another folder", () => {
    expect(step()).toEqual({ kind: "restore", directory: WORKTREE });
  });

  it("returns to the workspace for a workspace visit while a worktree shows", () => {
    expect(step({ visitRoot: null, shownRoot: WORKTREE, directory: `${WORKTREE}/src` })).toEqual({
      kind: "restore",
      directory: null,
    });
  });

  it("leaves a folder inside the visit's repository where it is", () => {
    expect(step({ shownRoot: WORKTREE, directory: `${WORKTREE}/src` })).toEqual({ kind: "none" });
    expect(step({ visitRoot: null, shownRoot: null, directory: "/repo/src" })).toEqual({
      kind: "none",
    });
  });

  it("does nothing for a landing that fell, a hidden review, or a visit with no comparison", () => {
    expect(step({ stands: false })).toEqual({ kind: "none" });
    expect(step({ active: false })).toEqual({ kind: "none" });
    expect(step({ hasComparison: false })).toEqual({ kind: "none" });
  });

  it("holds the visit until the folder's answers load", () => {
    expect(step({ resolved: false })).toEqual({ kind: "hold" });
    expect(step({ resolved: false, canChoose: false })).toEqual({ kind: "hold" });
  });

  it("holds before comparing folders, which read as the workspace until the worktrees load", () => {
    expect(
      step({ resolved: false, visitRoot: null, shownRoot: null, directory: WORKTREE }),
    ).toEqual({ kind: "hold" });
  });

  it("keeps the current folder when the visit's worktree is gone", () => {
    expect(step({ visitRootShows: false })).toEqual({ kind: "none" });
  });

  it("keeps the shown folder for a viewer who can't choose folders", () => {
    expect(step({ canChoose: false })).toEqual({ kind: "none" });
  });

  it("doesn't restore again once the folder is chosen but its worktree is gone", () => {
    expect(step({ directory: WORKTREE, shownRoot: null })).toEqual({ kind: "none" });
  });

  it("reads one folder spelled two ways as the same folder", () => {
    expect(step({ shownRoot: `${WORKTREE}/` })).toEqual({ kind: "none" });
    expect(step({ directory: `${WORKTREE}/`, shownRoot: null })).toEqual({ kind: "none" });
    expect(
      step({
        visitRoot: "C:\\repo\\.worktrees\\feature",
        shownRoot: "c:/repo/.worktrees/feature",
      }),
    ).toEqual({ kind: "none" });
  });
});
