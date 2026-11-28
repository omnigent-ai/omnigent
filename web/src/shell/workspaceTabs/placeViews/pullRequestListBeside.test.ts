import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  listShownOnOpen,
  onlyLinkedPullRequest,
  pullRequestListChosen,
  pullRequestOpening,
  resetPullRequestListBesideForTests,
  sessionPullRequestsTarget,
  showSessionPullRequests,
  usePullRequestListShown,
} from "./pullRequestListBeside";

const PR = "https://github.com/o/r/pull/12";
const OTHER = "https://github.com/o/r/pull/13";

beforeEach(() => resetPullRequestListBesideForTests());

const shown = (sessionId = "conv_1") => renderHook(() => usePullRequestListShown(sessionId)).result;

describe("the one linked pull request", () => {
  it("is the session's only PR, and none with none or several", () => {
    expect(onlyLinkedPullRequest([PR])).toBe(PR);
    expect(onlyLinkedPullRequest([])).toBeNull();
    expect(onlyLinkedPullRequest([PR, OTHER])).toBeNull();
  });

  it("opens on its own beside the list, while several open the list itself", () => {
    expect(sessionPullRequestsTarget([PR])).toEqual({
      kind: "pull",
      url: PR,
      list: true,
      overview: true,
    });
    expect(sessionPullRequestsTarget([PR, OTHER])).toEqual({ kind: "overview" });
    expect(sessionPullRequestsTarget([])).toEqual({ kind: "overview" });
  });

  it("opens the list, not one PR's details, when a sub-agent's PR is among them", () => {
    const scope = { includesSubAgent: true };
    expect(sessionPullRequestsTarget([PR], scope)).toEqual({ kind: "overview" });
    expect(sessionPullRequestsTarget([PR, OTHER], scope)).toEqual({ kind: "overview" });
    expect(sessionPullRequestsTarget([PR], { includesSubAgent: false })).toEqual({
      kind: "pull",
      url: PR,
      list: true,
      overview: true,
    });
  });
});

describe("listShownOnOpen", () => {
  it.each([
    [1, undefined, false],
    [2, undefined, true],
    [3, undefined, true],
    // An unknown count keeps today's list.
    [undefined, undefined, true],
    // An earlier open doesn't decide the next one; the person's choice does.
    [2, { shown: false, chosen: false }, true],
    [1, { shown: true, chosen: false }, false],
    [1, { shown: true, chosen: true }, true],
    [2, { shown: false, chosen: true }, false],
  ])("with %s linked and %o: shown %s", (count, current, expected) => {
    expect(listShownOnOpen(count, current)).toBe(expected);
  });
});

describe("the session's list beside its pull request", () => {
  it("is shown until an open folds it for the only PR", () => {
    const result = shown();
    expect(result.current).toBe(true);
    act(() => pullRequestOpening("conv_1", 1));
    expect(result.current).toBe(false);
    // Another session's list is its own.
    expect(shown("conv_2").current).toBe(true);
  });

  it("never unfolds on its own when a second PR is linked; the next open decides", () => {
    const result = shown();
    act(() => pullRequestOpening("conv_1", 1));
    // Linking reads nothing here: the list stays folded until a PR opens again.
    expect(result.current).toBe(false);
    act(() => pullRequestOpening("conv_1", 2));
    expect(result.current).toBe(true);
  });

  it("keeps the person's choice through later opens, either way", () => {
    const result = shown();
    act(() => pullRequestOpening("conv_1", 1));
    act(() => pullRequestListChosen("conv_1", true));
    act(() => pullRequestOpening("conv_1", 1));
    expect(result.current).toBe(true);
    act(() => pullRequestListChosen("conv_1", false));
    act(() => pullRequestOpening("conv_1", 3));
    expect(result.current).toBe(false);
  });
});

describe("showSessionPullRequests", () => {
  it("opens the only PR on Overview with the list folded", () => {
    const show = vi.fn();
    const result = shown();
    act(() => showSessionPullRequests(show, "conv_1", [PR], { classic: "open" }));
    expect(show).toHaveBeenCalledWith(
      { kind: "pull", url: PR, list: true, overview: true },
      { classic: "open" },
    );
    expect(result.current).toBe(false);
  });

  it("opens all of several, leaving the list as it is", () => {
    const show = vi.fn();
    const result = shown();
    act(() => pullRequestOpening("conv_1", 1));
    act(() => showSessionPullRequests(show, "conv_1", [PR, OTHER], { classic: "open" }));
    expect(show).toHaveBeenCalledWith({ kind: "overview" }, { classic: "open" });
    expect(result.current).toBe(false);
  });

  it("opens the list for a sub-agent's only PR, leaving the list as it is", () => {
    const show = vi.fn();
    const result = shown();
    act(() =>
      showSessionPullRequests(
        show,
        "conv_1",
        [PR],
        { classic: "open" },
        { includesSubAgent: true },
      ),
    );
    expect(show).toHaveBeenCalledWith({ kind: "overview" }, { classic: "open" });
    expect(result.current).toBe(true);
  });

  it("opens the overview without a session to remember for", () => {
    const show = vi.fn();
    showSessionPullRequests(show, null, [PR], { classic: "open" });
    expect(show).toHaveBeenCalledWith({ kind: "overview" }, { classic: "open" });
  });
});
