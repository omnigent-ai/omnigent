/**
 * Tests for {@link worktreeKeptNote} — the Archived-page note explaining
 * why an archived session's git worktree was left in place.
 */

import { describe, expect, it } from "vitest";

import { worktreeKeptNote } from "./worktreeKept";

describe("worktreeKeptNote", () => {
  it("returns null when the label is absent or soft-cleared", () => {
    expect(worktreeKeptNote(undefined)).toBeNull();
    expect(worktreeKeptNote("")).toBeNull();
  });

  it("returns null for malformed JSON", () => {
    expect(worktreeKeptNote("not json")).toBeNull();
    expect(worktreeKeptNote("[1,2]")).toBeNull();
  });

  it("explains an in-use worktree", () => {
    expect(worktreeKeptNote('{"reason": "in_use"}')).toBe(
      "Worktree kept — another session is still using it.",
    );
  });

  it("explains an offline host", () => {
    expect(worktreeKeptNote('{"reason": "host_offline"}')).toBe(
      "Worktree kept — its host is offline, so it couldn't be checked.",
    );
  });

  it("explains a failed check", () => {
    expect(worktreeKeptNote('{"reason": "unknown"}')).toBe(
      "Worktree kept — Omnigent couldn't verify it was safe to remove.",
    );
  });

  it("lists every unsafe fact the host reported", () => {
    const note = worktreeKeptNote(
      JSON.stringify({
        dirty_files: 2,
        unpushed_commits: 3,
        merged: false,
        default_ref: "origin/main",
      }),
    );
    expect(note).toBe(
      "Worktree kept — 2 uncommitted changes, 3 unpushed commits, " +
        "branch not merged into origin/main.",
    );
  });

  it("singularizes single counts", () => {
    const note = worktreeKeptNote(
      JSON.stringify({ dirty_files: 1, unpushed_commits: 1, merged: true }),
    );
    expect(note).toBe("Worktree kept — 1 uncommitted change, 1 unpushed commit.");
  });

  it("omits the merge claim when the branch is merged", () => {
    const note = worktreeKeptNote(
      JSON.stringify({ dirty_files: 1, unpushed_commits: 0, merged: true }),
    );
    expect(note).toBe("Worktree kept — 1 uncommitted change.");
  });

  it("says so when the merge status is undeterminable", () => {
    const note = worktreeKeptNote(
      JSON.stringify({ dirty_files: 0, unpushed_commits: 0, merged: null }),
    );
    expect(note).toBe("Worktree kept — merge status couldn't be determined.");
  });

  it("omits the default ref when absent", () => {
    const note = worktreeKeptNote(
      JSON.stringify({ dirty_files: 0, unpushed_commits: 0, merged: false }),
    );
    expect(note).toBe("Worktree kept — branch not merged.");
  });

  it("returns null for an all-clear shape (nothing unsafe to explain)", () => {
    const note = worktreeKeptNote(
      JSON.stringify({
        dirty_files: 0,
        unpushed_commits: 0,
        merged: true,
        default_ref: "origin/main",
      }),
    );
    expect(note).toBeNull();
  });
});
