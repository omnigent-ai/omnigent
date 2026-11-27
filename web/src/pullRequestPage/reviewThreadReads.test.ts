import { describe, expect, it } from "vitest";

import type { PullRequestSubtab } from "@/shell/pullRequestPanelView";

import { reviewThreadsWanted } from "./reviewThreadReads";

const ready = { active: true, readable: true, hasPr: true };
const tabs: PullRequestSubtab[] = ["summary", "comments", "commits", "changes"];

describe("reviewThreadsWanted", () => {
  it("reads threads on the new page's Overview, Comments and Changes, never its Commits", () => {
    const wanted = tabs.filter((tab) => reviewThreadsWanted({ ...ready, tab, newPage: true }));
    expect(wanted).toEqual(["summary", "comments", "changes"]);
  });

  it("reads them only on Changes on the classic page", () => {
    const wanted = tabs.filter((tab) => reviewThreadsWanted({ ...ready, tab, newPage: false }));
    expect(wanted).toEqual(["changes"]);
  });

  it("reads nothing while hidden, unreadable or without a pull request", () => {
    for (const blocked of [{ active: false }, { readable: false }, { hasPr: false }]) {
      expect(reviewThreadsWanted({ ...ready, ...blocked, tab: "summary", newPage: true })).toBe(
        false,
      );
    }
  });
});
