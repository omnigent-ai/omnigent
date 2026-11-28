import { describe, expect, it, vi } from "vitest";

import { stackDiffReviewBase, stackDiffStatusText } from "./stackDiffCommands";
import { comparison } from "./stackDiffFixtures";
import type { EffectiveStackDiff, StackDiffIntent } from "./stackDiffMode";
import type { PullRequestStackDiff } from "./usePullRequestStackDiff";

function diffOf(
  effective: EffectiveStackDiff,
  intent: StackDiffIntent = "automatic",
): PullRequestStackDiff {
  return {
    parent: { number: 100, label: "#100", headRepository: "acme/app" },
    intent,
    choose: vi.fn(),
    effective,
    diagnostic: null,
    reload: vi.fn(),
    retry: vi.fn(),
  };
}

describe("stackDiffStatusText", () => {
  it.each<[EffectiveStackDiff, string]>([
    [{ kind: "comparing" }, "Comparing with #100…"],
    [{ kind: "stale" }, "This stack changed since it was compared."],
    [{ kind: "this-pr", comparison: comparison() }, "Only this PR's changes, since #100"],
    [{ kind: "all", fallback: null }, "All changes since the base branch"],
    [{ kind: "all", fallback: "parent_closed" }, "#100 was closed. Showing all changes."],
  ])("says what %o shows", (effective, text) => {
    expect(stackDiffStatusText(diffOf(effective))).toBe(text);
  });
});

describe("stackDiffReviewBase", () => {
  it("names the parent where only this PR's changes show, and checks the choice made", () => {
    const diff = diffOf({ kind: "this-pr", comparison: comparison() });
    const base = stackDiffReviewBase(diff);
    expect(base).toMatchObject({
      label: "vs #100",
      description: "Only this PR's changes, since #100",
      heading: "Show",
    });
    expect(base.choices?.map((choice) => [choice.label, choice.checked])).toEqual([
      ["Automatic", true],
      ["This PR only", false],
      ["All changes", false],
    ]);
    expect(base.choices?.[1]?.description).toBe("Changes since #100");
    expect(base).not.toHaveProperty("actions");
    base.choices?.[2]?.onSelect();
    expect(diff.choose).toHaveBeenCalledWith("all");
  });

  it("names the base branch where all changes show, with the reason a choice fell back", () => {
    const base = stackDiffReviewBase(diffOf({ kind: "all", fallback: "not_carried" }, "this-pr"));
    expect(base.label).toBe("vs base branch");
    expect(base.description).toContain("can't be told apart");
    expect(base).not.toHaveProperty("actions");
  });

  it("offers Try Again after a failed comparison, and Reload once the stack changed", () => {
    const failed = diffOf({ kind: "all", fallback: "failed" }, "this-pr");
    const retry = stackDiffReviewBase(failed).actions!;
    expect(retry.map((action) => action.label)).toEqual(["Compare with #100 again"]);
    retry[0]!.onSelect();
    expect(failed.retry).toHaveBeenCalledOnce();

    const stale: PullRequestStackDiff = {
      ...diffOf({ kind: "all", fallback: null }),
      diagnostic: { kind: "stale" },
    };
    const reload = stackDiffReviewBase(stale).actions!;
    expect(reload.map((action) => action.label)).toEqual(["Reload stack status"]);
    reload[0]!.onSelect();
    expect(stale.reload).toHaveBeenCalledOnce();
  });
});
