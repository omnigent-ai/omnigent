import { afterEach, describe, expect, it, vi } from "vitest";

import {
  sessionChoiceCount,
  setWorkingCopyComparisonsForTests,
  subscribeSessionComparisons,
  workingCopyComparisonsAt,
  workspaceKeyOf,
} from "./workingCopyComparisons";

const SESSION = "conv_owners";
const WORKSPACE = workspaceKeyOf("host_1", "/repo");

afterEach(() => {
  setWorkingCopyComparisonsForTests(null);
});

describe("session comparisons across the workspace's and folders' owners", () => {
  it("tells a listener about a folder whose owner is made after it subscribed", () => {
    const listener = vi.fn();
    const stop = subscribeSessionComparisons(SESSION, listener);

    const folder = workingCopyComparisonsAt(".worktrees/feature", WORKSPACE);
    folder.chose(SESSION);
    expect(sessionChoiceCount(SESSION)).toBe(1);
    expect(listener).toHaveBeenCalled();

    // Once stopped, neither that folder nor one made later reaches it.
    stop();
    listener.mockClear();
    folder.chose(SESSION);
    workingCopyComparisonsAt(".worktrees/other", WORKSPACE).chose(SESSION);
    expect(sessionChoiceCount(SESSION)).toBe(3);
    expect(listener).not.toHaveBeenCalled();
  });

  it("keeps one owner per folder and workspace", () => {
    const folder = workingCopyComparisonsAt(".worktrees/feature", WORKSPACE);
    expect(workingCopyComparisonsAt(".worktrees/feature", WORKSPACE)).toBe(folder);
    expect(
      workingCopyComparisonsAt(".worktrees/feature", workspaceKeyOf("host_2", "/repo")),
    ).not.toBe(folder);
  });
});
