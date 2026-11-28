import { describe, expect, it } from "vitest";

import {
  absoluteWorkspacePath,
  normalizeComposerContextState,
  type ComposerContextState,
} from "./composerContext";
import {
  composerContextFromLabels,
  composerContextFromMetadata,
  composerContextToLabel,
  composerContextToLabels,
  composerContextToMetadata,
} from "./composerContextAdapters";

const state: ComposerContextState = {
  workingDirectory: { kind: "selected", path: "/repo" },
  worktree: { kind: "new", branchName: "feature/context", baseBranch: "main" },
};

describe("composer context", () => {
  it("clears worktree selection only when the working directory is known non-Git", () => {
    expect(normalizeComposerContextState(state, "unknown").worktree.kind).toBe("new");
    expect(normalizeComposerContextState(state, "not_git").worktree).toEqual({ kind: "none" });
  });

  it("round-trips persisted metadata including intentional empty selections", () => {
    expect(composerContextFromMetadata(composerContextToMetadata(state))).toEqual(state);

    const empty = normalizeComposerContextState({
      workingDirectory: { kind: "unset" },
      worktree: { kind: "none" },
    });
    expect(composerContextFromMetadata(composerContextToMetadata(empty))).toEqual(empty);
  });

  it("round-trips session labels and ignores malformed metadata", () => {
    expect(
      composerContextFromLabels({
        "omnigent.composer_context.v1": composerContextToLabel(state),
      }),
    ).toEqual(state);
    expect(composerContextFromLabels(composerContextToLabels(state))).toEqual(state);
    expect(composerContextFromLabels({ "omnigent.composer_context.v1": "not-json" })).toEqual({
      workingDirectory: { kind: "unset" },
      worktree: { kind: "none" },
    });
  });

  it("serializes only the persisted workspace and worktree state", () => {
    expect(composerContextToMetadata(state)).toEqual({
      version: 1,
      working_directory: { path: "/repo" },
      worktree: { mode: "new", branch_name: "feature/context", base_branch: "main" },
    });
  });
});

describe("absoluteWorkspacePath", () => {
  it.each([
    ["docs/a.md", "/repo", "/repo/docs/a.md"],
    ["docs/a.md", "/repo/", "/repo/docs/a.md"],
    ["notes.md", "/", "/notes.md"],
    ["docs\\a.md", "C:\\work\\", "C:\\work\\docs\\a.md"],
    ["/tmp/a.md", "/repo", "/tmp/a.md"],
    ["C:\\tmp\\a.md", "/repo", "C:\\tmp\\a.md"],
  ])("joins %s onto root %s as %s", (path, root, expected) => {
    expect(absoluteWorkspacePath(path, root)).toBe(expected);
  });

  it("keeps an absolute path without a root but cannot place a relative one", () => {
    expect(absoluteWorkspacePath("/tmp/a.md", null)).toBe("/tmp/a.md");
    expect(absoluteWorkspacePath("docs/a.md", null)).toBeNull();
  });
});
