// Which merge options let a row's Enable Auto-merge… be turned on, and why not the others.

import { describe, expect, it } from "vitest";

import type { PullRequestMergeOptions } from "@/newPullRequestPage/hooks/usePullRequests";

import { autoMergeRefusedBy } from "./EnableAutoMergeDialog";

const PR = "https://github.com/acme/app/pull/42";
const HEAD = "a".repeat(40);
const OPTIONS: PullRequestMergeOptions = {
  object: "session.github.merge_options",
  methods: ["squash", "merge"],
  default: "squash",
  merge_queue: false,
  merge_state_status: "BLOCKED",
  auto_merge: null,
};

describe("autoMergeRefusedBy", () => {
  const pr = { url: PR, headSha: HEAD };
  it("lets it be turned on for a blocked or behind PR on a base without a queue", () => {
    expect(autoMergeRefusedBy(OPTIONS, pr)).toBeNull();
    expect(autoMergeRefusedBy({ ...OPTIONS, merge_state_status: "BEHIND" }, pr)).toBeNull();
  });

  it("says why for each state it can't be turned on in", () => {
    expect(autoMergeRefusedBy({ ...OPTIONS, methods: [], default: null }, pr)).toBe(
      "The repository allows no merge method to turn auto-merge on with.",
    );
    expect(autoMergeRefusedBy({ ...OPTIONS, merge_state_status: "UNSTABLE" }, pr)).toMatch(
      /can merge now/,
    );
    expect(autoMergeRefusedBy({ ...OPTIONS, merge_state_status: "DIRTY" }, pr)).toBe(
      "GitHub can’t turn on auto-merge for it now. Check it on GitHub.",
    );
  });
});
