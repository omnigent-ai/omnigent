import { describe, expect, it } from "vitest";

import { worktreeKeptNote } from "./worktreeKept";

describe("worktreeKeptNote", () => {
  it("returns null for missing or malformed labels", () => {
    expect(worktreeKeptNote(undefined)).toBeNull();
    expect(worktreeKeptNote("not json")).toBeNull();
  });

  it("explains broad reasons the worktree was kept", () => {
    expect(worktreeKeptNote('{"reason":"in_use"}')).toBe(
      "Worktree kept — another session is still using it.",
    );
    expect(worktreeKeptNote('{"reason":"host_offline"}')).toBe(
      "Worktree kept — its host is offline, so it couldn't be checked.",
    );
    expect(worktreeKeptNote('{"reason":"unknown"}')).toBe(
      "Worktree kept — Omnigent couldn't verify it was safe to remove.",
    );
  });

  it("lists changes, unpushed commits, and merge status", () => {
    expect(
      worktreeKeptNote(
        JSON.stringify({
          dirty_files: 2,
          unpushed_commits: 1,
          merged: false,
          default_ref: "origin/main",
        }),
      ),
    ).toBe(
      "Worktree kept — 2 uncommitted changes, 1 unpushed commit, " +
        "branch not merged into origin/main.",
    );
  });

  it("reports indeterminate merge state", () => {
    expect(worktreeKeptNote('{"merged":null}')).toBe(
      "Worktree kept — merge status couldn't be determined.",
    );
  });
});
