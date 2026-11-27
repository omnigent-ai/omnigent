import { describe, expect, it } from "vitest";

import { samePlace } from "../planOpen";
import {
  listedPullRequestChosen,
  pullRequestListShown,
  pullRequestNewTabPlace,
  pullRequestPanelViewFor,
  pullRequestPlaceFor,
  pullRequestViewChanged,
  shownPullRequestPlace,
  unshownPullRequestParts,
} from "./pullRequestPlaceActions";

const PR = "https://github.com/o/r/pull/12";

describe("pull request tab", () => {
  it("maps places to panel views and back", () => {
    expect(pullRequestPanelViewFor({ kind: "pulls" })).toEqual({
      showingDetails: false,
      subtab: "summary",
    });
    expect(
      pullRequestPanelViewFor({ kind: "pull", url: PR, view: "commits", commit: "abc1" }),
    ).toEqual({
      selected: PR,
      showingDetails: true,
      subtab: "commits",
      commit: "abc1",
    });
    expect(
      pullRequestPanelViewFor({ kind: "pull", url: PR, view: "changes", commit: "abc1" }),
    ).toEqual({
      selected: PR,
      showingDetails: true,
      subtab: "changes",
    });
    expect(pullRequestPlaceFor({ selected: PR, showingDetails: false, subtab: "summary" })).toEqual(
      {
        kind: "pulls",
      },
    );
    expect(pullRequestPlaceFor({ showingDetails: true, subtab: "summary" })).toEqual({
      kind: "pulls",
    });
  });

  it("steps a tab with the list beside it to the chosen pull request, on the same sub-view", () => {
    const other = "https://github.com/o/r/pull/13";
    expect(listedPullRequestChosen({ kind: "pulls" }, PR)).toEqual({
      type: "navigate",
      place: { kind: "pull", url: PR, view: "summary", list: true },
      replace: false,
    });
    // A review moves on to the next pull request's changes, not one file or commit of them.
    expect(
      listedPullRequestChosen(
        { kind: "pull", url: PR, view: "changes", path: "a.ts", list: true },
        other,
      ),
    ).toEqual({
      type: "navigate",
      place: { kind: "pull", url: other, view: "changes", list: true },
      replace: false,
    });
    // From the list alone, on the section last shown here, a commit of the PR it left left behind.
    expect(listedPullRequestChosen({ kind: "pulls" }, other, "commits")).toEqual({
      type: "navigate",
      place: { kind: "pull", url: other, view: "commits", list: true },
      replace: false,
    });
    expect(
      listedPullRequestChosen(
        { kind: "pull", url: PR, view: "commits", commit: "abc1", list: true },
        other,
        "changes",
      ),
    ).toEqual({
      type: "navigate",
      place: { kind: "pull", url: other, view: "commits", list: true },
      replace: false,
    });
    // Choosing the one on screen again adds no step.
    expect(
      listedPullRequestChosen({ kind: "pull", url: PR, view: "summary", list: true }, PR),
    ).toEqual({ type: "none" });
    expect(pullRequestListShown()).toEqual({
      type: "navigate",
      place: { kind: "pulls" },
      replace: false,
    });
  });

  it("keeps the list beside a pull request through its sub-views", () => {
    const current = { kind: "pull", url: PR, view: "summary", list: true } as const;
    expect(
      pullRequestViewChanged(
        current,
        { selected: PR, showingDetails: true, subtab: "changes" },
        false,
        samePlace,
      ),
    ).toEqual({
      type: "navigate",
      place: { kind: "pull", url: PR, view: "changes", list: true },
      replace: false,
    });
    // The view the place already shows is no step, list or not.
    expect(
      pullRequestViewChanged(
        current,
        { selected: PR, showingDetails: true, subtab: "summary" },
        false,
        samePlace,
      ),
    ).toEqual({ type: "none" });
    expect(shownPullRequestPlace({ ...current, focus: "checks" })).toEqual(current);
  });

  it("records a sub-view change as a step, and never turns a tab into another place", () => {
    const current = { kind: "pull", url: PR, view: "summary" } as const;
    expect(
      pullRequestViewChanged(
        current,
        { selected: PR, showingDetails: true, subtab: "comments" },
        false,
        samePlace,
      ),
    ).toEqual({
      type: "navigate",
      place: { kind: "pull", url: PR, view: "comments" },
      replace: false,
    });
    // The overview stays the list, a pull request's tab stays that pull request:
    // another place opens its own tab instead.
    expect(
      pullRequestViewChanged(
        { kind: "pulls" },
        { selected: PR, showingDetails: true, subtab: "summary" },
        true,
        samePlace,
      ),
    ).toEqual({ type: "none" });
    expect(
      pullRequestViewChanged(
        current,
        { showingDetails: false, subtab: "summary" },
        false,
        samePlace,
      ),
    ).toEqual({ type: "none" });
    expect(
      pullRequestViewChanged(
        current,
        { selected: `${PR}0`, showingDetails: true, subtab: "summary" },
        true,
        samePlace,
      ),
    ).toEqual({ type: "none" });
    // A correction within the same pull request still replaces.
    expect(
      pullRequestViewChanged(
        current,
        { selected: PR, showingDetails: true, subtab: "commits" },
        true,
        samePlace,
      ),
    ).toEqual({
      type: "navigate",
      place: { kind: "pull", url: PR, view: "commits" },
      replace: true,
    });
  });

  it("ignores reports of the view already shown, keeping the place's file cursor", () => {
    const atFile = { kind: "pull", url: PR, view: "changes", path: "a.ts" } as const;
    expect(
      pullRequestViewChanged(
        atFile,
        { selected: PR, showingDetails: true, subtab: "changes", file: { path: "a.ts" } },
        true,
        samePlace,
      ),
    ).toEqual({ type: "none" });
    expect(
      pullRequestViewChanged(
        { kind: "pulls" },
        { selected: PR, showingDetails: false, subtab: "summary" },
        true,
        samePlace,
      ),
    ).toEqual({ type: "none" });
  });

  it("maps a file in Changes to the panel and back, as a step", () => {
    const place = {
      kind: "pull",
      url: PR,
      view: "changes",
      path: "a.ts",
      line: { start: 4, end: 6 },
    } as const;
    expect(pullRequestPanelViewFor(place)).toEqual({
      selected: PR,
      showingDetails: true,
      subtab: "changes",
      file: { path: "a.ts" },
    });
    expect(
      pullRequestViewChanged(
        { kind: "pull", url: PR, view: "changes" },
        { selected: PR, showingDetails: true, subtab: "changes", file: { path: "b.ts" } },
        false,
        samePlace,
      ),
    ).toEqual({
      type: "navigate",
      place: { kind: "pull", url: PR, view: "changes", path: "b.ts" },
      replace: false,
    });
    expect(
      pullRequestPanelViewFor({ kind: "pull", url: PR, view: "summary", path: "a.ts" }),
    ).toEqual({
      selected: PR,
      showingDetails: true,
      subtab: "summary",
    });
  });
});

describe("pull request parts the tab can't show yet", () => {
  it.each([
    [
      { kind: "pull", url: PR, view: "summary", path: "a.ts", line: { start: 4 } },
      "This link points at a.ts line 4, which this tab can't open yet. It shows the pull request's overview instead.",
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
      { kind: "pull", url: PR, view: "comments", commit: "abcdef123" },
      "This link points at commit abcdef1, which this tab can't open yet. It shows the pull request's comments instead.",
    ],
    [
      { kind: "pull", url: PR, view: "summary", focus: "checks" },
      "This link points at the checks, which this tab can't open yet. It shows the pull request's overview instead.",
    ],
  ] as const)("names everything in %o", (place, text) => {
    expect(unshownPullRequestParts(place)).toBe(text);
    expect(shownPullRequestPlace(place)).toEqual({ kind: "pull", url: PR, view: place.view });
  });

  it("shows a file in Changes itself but says it can't go to a line there", () => {
    const place = {
      kind: "pull",
      url: PR,
      view: "changes",
      path: "a.ts",
      line: { start: 4 },
    } as const;
    expect(unshownPullRequestParts(place)).toBe(
      "This link points at line 4 of a.ts, which this tab can't open yet. It shows the top of a.ts instead.",
    );
    expect(shownPullRequestPlace(place)).toEqual({
      kind: "pull",
      url: PR,
      view: "changes",
      path: "a.ts",
    });
    expect(
      unshownPullRequestParts({ kind: "pull", url: PR, view: "changes", path: "a.ts" }),
    ).toBeNull();
  });

  it("has nothing to say about places the panel shows", () => {
    expect(unshownPullRequestParts({ kind: "pulls" })).toBeNull();
    expect(
      unshownPullRequestParts({ kind: "pull", url: PR, view: "commits", commit: "abc1" }),
    ).toBeNull();
    expect(
      shownPullRequestPlace({ kind: "pull", url: PR, view: "commits", commit: "abc1" }),
    ).toEqual({
      kind: "pull",
      url: PR,
      view: "commits",
      commit: "abc1",
    });
  });
});

describe("pullRequestNewTabPlace", () => {
  it("names one commit in Commits, or one file in Changes, of the pull request", () => {
    expect(pullRequestNewTabPlace(PR, { commit: "abc1" })).toEqual({
      kind: "pull",
      url: PR,
      view: "commits",
      commit: "abc1",
    });
    const file = pullRequestNewTabPlace(PR, { path: "src/a.ts" });
    expect(file).toEqual({ kind: "pull", url: PR, view: "changes", path: "src/a.ts" });
    // The tab shows all of it: the panel opens that file's cursor, with nothing left unshown.
    expect(unshownPullRequestParts(file)).toBeNull();
  });
});
