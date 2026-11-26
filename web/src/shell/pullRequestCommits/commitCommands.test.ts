import { describe, expect, it } from "vitest";

import {
  BRANCH_COMMIT_KEYWORDS,
  commitCommands,
  FIRST_COMMIT_REASON,
  LAST_COMMIT_REASON,
  LAST_SHOWN_COMMIT_REASON,
  NEWEST_COMMIT_REASON,
  OLDEST_COMMIT_REASON,
  UNLISTED_COMMIT_REASON,
  type CommitCommandFacts,
  type OpenCommitFacts,
} from "./commitCommands";

const MIDDLE: OpenCommitFacts = {
  sha: "b",
  listed: true,
  previousSha: "a",
  nextSha: "c",
  moreAfterList: false,
};
const OPEN_MIDDLE: CommitCommandFacts = { tabShown: true, onCommitsTab: true, open: MIDDLE };

const open = (facts: Partial<OpenCommitFacts>): CommitCommandFacts => ({
  ...OPEN_MIDDLE,
  open: { ...MIDDLE, ...facts },
});
const byId = (facts: CommitCommandFacts) =>
  Object.fromEntries(commitCommands(facts).map((command) => [command.id, command]));

describe("commitCommands", () => {
  it("offers nothing where the pull request has no Commits tab", () => {
    expect(commitCommands({ ...OPEN_MIDDLE, tabShown: false })).toEqual([]);
  });

  it("offers Show commits from another tab, which moves focus to the list", () => {
    const [command, ...rest] = commitCommands({ ...OPEN_MIDDLE, onCommitsTab: false });
    expect(rest).toEqual([]);
    expect(command).toMatchObject({
      id: "show-commits",
      paletteLabel: "Show commits",
      availability: { available: true },
      movesFocus: true,
    });
  });

  it("offers nothing more on the list itself, whose rows take the arrow keys", () => {
    expect(commitCommands({ ...OPEN_MIDDLE, open: null })).toEqual([]);
  });

  it("offers stepping, the list, and Changes while a commit is open, each moving focus", () => {
    expect(commitCommands(OPEN_MIDDLE).map((command) => command.paletteLabel)).toEqual([
      "Go to the previous commit",
      "Go to the next commit",
      "Show all commits",
      "Show all changes",
    ]);
    const commands = byId(OPEN_MIDDLE);
    expect(commands["previous-commit"]).toMatchObject({
      targetSha: "a",
      buttonLabel: "Previous commit",
    });
    expect(commands["next-commit"]).toMatchObject({ targetSha: "c", buttonLabel: "Next commit" });
    expect(commands["all-commits"]!.buttonLabel).toBe("All commits");
    expect(commands["all-changes"]!.buttonLabel).toBe("All changes");
    // Each replaces what has focus, so ⌘K closes before it runs.
    expect(commitCommands(OPEN_MIDDLE).every((command) => command.movesFocus)).toBe(true);
  });

  it("greys out stepping past either end of a complete list, saying why", () => {
    const first = byId(open({ previousSha: null }));
    expect(first["previous-commit"]).toMatchObject({
      availability: { available: false, reason: FIRST_COMMIT_REASON },
    });
    expect(first["previous-commit"]).not.toHaveProperty("targetSha");
    expect(first["next-commit"]!.availability).toEqual({ available: true });

    const last = byId(open({ nextSha: null }));
    expect(last["next-commit"]).toMatchObject({
      availability: { available: false, reason: LAST_COMMIT_REASON },
    });
    expect(last["next-commit"]).not.toHaveProperty("targetSha");
  });

  it("doesn't call the last row of a truncated list the PR's last commit", () => {
    const last = byId(open({ nextSha: null, moreAfterList: true }));
    expect(last["next-commit"]!.availability).toEqual({
      available: false,
      reason: LAST_SHOWN_COMMIT_REASON,
    });
  });

  it("says a commit missing from the list has no neighbours there, rather than first or last", () => {
    const unlisted = byId(open({ listed: false, previousSha: null, nextSha: null }));
    for (const id of ["previous-commit", "next-commit"]) {
      expect(unlisted[id]!.availability).toEqual({
        available: false,
        reason: UNLISTED_COMMIT_REASON,
      });
    }
    expect(unlisted["all-commits"]!.availability).toEqual({ available: true });
  });

  it("offers the open commit in a new tab only where commits can have their own tab", () => {
    const sha = "0123456789abcdef";
    expect(byId(open({ sha }))).not.toHaveProperty("open-in-new-tab");
    expect(byId({ ...open({ sha }), newTab: true })["open-in-new-tab"]).toEqual({
      id: "open-in-new-tab",
      buttonLabel: "Open in new tab",
      paletteLabel: "Open commit 0123456 in a new tab",
      keywords: expect.arrayContaining(["commit", "tab", "new"]),
      availability: { available: true },
      movesFocus: false,
      targetSha: sha,
    });
    // On the list there's no open commit to name.
    expect(
      commitCommands({ tabShown: true, onCommitsTab: true, open: null, newTab: true }),
    ).toEqual([]);
  });

  it("words a newest-first list of a branch's commits by age, matched by its own keywords", () => {
    const branch = { keywords: BRANCH_COMMIT_KEYWORDS, order: "newest-first" } as const;
    const middle = byId({ ...OPEN_MIDDLE, ...branch });
    expect(middle["previous-commit"]!.keywords).toEqual(
      expect.arrayContaining(["branch", "newer", "up"]),
    );
    expect(middle["previous-commit"]!.keywords).not.toContain("pull request");
    expect(middle["next-commit"]!.keywords).toEqual(expect.arrayContaining(["older", "down"]));
    const newest = byId({ ...open({ previousSha: null }), ...branch });
    expect(newest["previous-commit"]!.availability).toEqual({
      available: false,
      reason: NEWEST_COMMIT_REASON,
    });
    const oldest = byId({ ...open({ nextSha: null }), ...branch });
    expect(oldest["next-commit"]!.availability).toEqual({
      available: false,
      reason: OLDEST_COMMIT_REASON,
    });
    // A cut-off list still says only that it's the last shown.
    const cutOff = byId({ ...open({ nextSha: null, moreAfterList: true }), ...branch });
    expect(cutOff["next-commit"]!.availability).toEqual({
      available: false,
      reason: LAST_SHOWN_COMMIT_REASON,
    });
  });
});
