import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RevisionSpec } from "./customComparison";
import type { BranchPointSupport } from "./workingCopyComparison";
import {
  CHECKING_REVISIONS,
  LAST_COMMIT_REVISIONS,
  UNKNOWN_BASELINE_TITLE,
  workingCopyBaseCommands,
} from "./workingCopyBaseCommands";
import { readWorkingCopyBaseIntent } from "./workingCopyBasePreference";
import type { WorkingCopyComparisonOwner } from "./workingCopyComparisonOwner";

afterEach(() => window.localStorage.clear());

function commands({
  custom = null,
  support = { kind: "available" },
  revisionSupport = { kind: "available" },
  headIsKnown,
}: {
  custom?: RevisionSpec | null;
  support?: BranchPointSupport;
  revisionSupport?: BranchPointSupport;
  headIsKnown?: boolean;
} = {}) {
  const owner = { chose: vi.fn(), refresh: vi.fn(), compareRevisions: vi.fn() };
  const list = workingCopyBaseCommands({
    sessionId: "conv_1",
    intent: "head",
    custom,
    support,
    revisionSupport,
    headIsKnown,
    owner: owner as unknown as WorkingCopyComparisonOwner,
    queryClient: new QueryClient(),
  });
  const find = (id: string) => list.find((command) => command.id === id)!;
  return { owner, list, find };
}

describe("workingCopyBaseCommands", () => {
  it("names the choices the way each surface writes them", () => {
    expect(commands().list.map(({ label, name }) => [label, name])).toEqual([
      ["Automatic", "Automatic"],
      ["Uncommitted Changes", "Uncommitted changes"],
      ["All Branch Changes", "All branch changes"],
      ["Last Commit", "Last commit"],
      ["Refresh Changes", "Refresh changes"],
    ]);
  });

  it("checks the saved base, or Last Commit for its typed comparison, and nothing for another", () => {
    const checked = (custom: RevisionSpec | null) =>
      commands({ custom })
        .list.filter((command) => command.checked)
        .map((command) => command.id);
    expect(checked(null)).toEqual(["compare-head"]);
    expect(checked(LAST_COMMIT_REVISIONS)).toEqual(["compare-last-commit"]);
    expect(checked({ base: "v1.2", head: "" })).toEqual([]);
  });

  it("saves the chosen base and tells the owner, which ends a typed comparison", () => {
    const { owner, find } = commands({ custom: { base: "v1.2", head: "" } });
    find("compare-branch-point").run();
    expect(readWorkingCopyBaseIntent()).toBe("branch-point");
    expect(owner.chose).toHaveBeenCalledWith("conv_1");
  });

  it("compares the last commit with its parent, without changing the saved base", () => {
    const { owner, find } = commands();
    find("compare-last-commit").run();
    expect(owner.compareRevisions).toHaveBeenCalledWith("conv_1", {
      base: "HEAD~1",
      head: "HEAD",
    });
    expect(owner.chose).not.toHaveBeenCalled();
    expect(readWorkingCopyBaseIntent()).toBe("automatic");
  });

  it("says why each choice can't run, from the support it needs", () => {
    const reasons = (input: Parameters<typeof commands>[0]) =>
      Object.fromEntries(
        commands(input).list.map((command) => [command.id, command.unavailableReason]),
      );
    expect(reasons({})).toEqual({
      "compare-automatic": null,
      "compare-head": null,
      "compare-branch-point": null,
      "compare-last-commit": null,
      "refresh-changes": null,
    });
    expect(
      reasons({
        headIsKnown: false,
        support: { kind: "unsupported", message: "Update the app.", reason: "old-app" },
        revisionSupport: {
          kind: "unsupported",
          message: "Typed revisions need the app.",
          reason: "old-app",
        },
      }),
    ).toEqual({
      "compare-automatic": null,
      "compare-head": UNKNOWN_BASELINE_TITLE,
      "compare-branch-point": "Update the app.",
      "compare-last-commit": "Typed revisions need the app.",
      "refresh-changes": null,
    });
    expect(reasons({ revisionSupport: { kind: "checking" } })["compare-last-commit"]).toBe(
      CHECKING_REVISIONS,
    );
  });
});
