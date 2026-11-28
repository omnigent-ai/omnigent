import { describe, expect, it, vi } from "vitest";

import type { WorkingCopyBaseCommand } from "@/lib/workingCopyBaseCommands";
import {
  type BranchPointComparison,
  type BranchPointSupport,
  COULD_NOT_REACH,
  effectiveComparison,
  type EffectiveComparison,
  RECONNECTING,
} from "@/lib/workingCopyComparison";
import type { WorkingCopyBaseIntent } from "@/lib/workingCopyBasePreference";
import {
  CHECKING_REVISIONS,
  COMPARISON_UNAVAILABLE,
  localComparisonView,
  type LocalComparisonInput,
  NO_ACCESS_MESSAGE,
  onComparisonChosen,
  SHOWING_HOST_CHANGES,
  UNKNOWN_BASE,
  UNKNOWN_BASELINE_TITLE,
} from "./localComparison";

const AVAILABLE: BranchPointSupport = { kind: "available" };
const BRANCH_POINT: BranchPointComparison = {
  id: "comparison-1",
  ref: "origin/main",
  baseOid: "a".repeat(40),
  headOid: "b".repeat(40),
  refOid: "c".repeat(40),
  entries: [],
  unrepresentableNames: 0,
  listedAt: 1,
};
const HEAD: EffectiveComparison = { base: "head", note: null, override: false, retry: false };

function commands(intent: WorkingCopyBaseIntent, branchPointReason: string | null = null) {
  const make = (
    id: WorkingCopyBaseCommand["id"],
    label: string,
    choice?: WorkingCopyBaseIntent,
  ): WorkingCopyBaseCommand => ({
    id,
    label,
    name: label.charAt(0) + label.slice(1).toLowerCase(),
    checked: choice ? intent === choice : undefined,
    unavailableReason: id === "compare-branch-point" ? branchPointReason : null,
    run: vi.fn(),
  });
  return [
    make("compare-automatic", "Automatic", "automatic"),
    make("compare-head", "Uncommitted Changes", "head"),
    make("compare-branch-point", "All Branch Changes", "branch-point"),
    make("compare-last-commit", "Last Commit"),
    make("refresh-changes", "Refresh Changes"),
  ];
}

function view(overrides: Partial<LocalComparisonInput> = {}) {
  const intent = overrides.intent ?? "automatic";
  const input: LocalComparisonInput = {
    comparison: HEAD,
    headIsKnown: true,
    revisionSupport: { kind: "unsupported", reason: "update_app", message: "Update the app." },
    custom: null,
    intent,
    commands: commands(intent),
    permitted: true,
    readable: true,
    actions: {
      refetchHead: vi.fn(),
      retryBranchPoint: vi.fn(),
      showLastCommit: vi.fn(),
      compareAgain: vi.fn(),
      compareRevisions: vi.fn(),
    },
    ...overrides,
  };
  return { input, result: localComparisonView(input) };
}

describe("the trigger", () => {
  it("names the branch point by its ref, compared with the working copy", () => {
    const { result } = view({
      comparison: { base: "branch-point", comparison: BRANCH_POINT, status: null },
    });
    expect(result.trigger).toEqual({
      name: "All branch changes",
      base: "origin/main",
      head: "Working Copy",
      title: "origin/main compared with Working Copy",
      invalid: false,
    });
    expect(result.state).toEqual({ kind: "ready", status: null });
  });

  it("names HEAD only when the workspace is known to be Git", () => {
    expect(view().result.trigger).toMatchObject({ base: "HEAD", invalid: false });
    const unknown = view({ headIsKnown: false }).result;
    expect(unknown.trigger).toEqual({
      name: null,
      base: null,
      head: "Working Copy",
      title: UNKNOWN_BASELINE_TITLE,
      invalid: false,
    });
    const unknownPreset = unknown.presets.find((preset) => preset.id === "head");
    expect(unknownPreset).toMatchObject({
      label: UNKNOWN_BASE,
      unavailableReason: UNKNOWN_BASELINE_TITLE,
    });
  });

  it("marks an explicit branch point that can't be made invalid, and shows no other data", () => {
    const { result } = view({
      intent: "branch-point",
      comparison: {
        base: "unavailable",
        message: "No branch point with origin/main.",
        retry: false,
      },
    });
    expect(result.trigger.invalid).toBe(true);
    expect(result.state).toEqual({
      kind: "unavailable",
      title: COMPARISON_UNAVAILABLE,
      message: "No branch point with origin/main.",
      canRetry: false,
    });
    expect(result.fallback).toBeNull();
  });

  it("waits while the branch point is being compared", () => {
    const { result } = view({ comparison: { base: "pending", message: "Comparing…" } });
    expect(result.state).toEqual({ kind: "pending", message: "Comparing…" });
    expect(result.trigger.invalid).toBe(false);
  });
});

describe("retained changes", () => {
  it("carries an established branch point's reconnecting or stale status", () => {
    for (const status of [
      { kind: "reconnecting" as const, message: RECONNECTING },
      { kind: "stale" as const, message: "Couldn't refresh: offline" },
    ]) {
      const { result } = view({
        comparison: { base: "branch-point", comparison: BRANCH_POINT, status },
      });
      expect(result.state).toEqual({ kind: "ready", status });
    }
  });

  it("marks last-commit changes as reconnecting while the workspace can't be read", () => {
    expect(view({ readable: false }).result.state).toEqual({
      kind: "ready",
      status: { kind: "reconnecting", message: RECONNECTING },
    });
  });

  it("shows nothing retained once the viewer lost access", () => {
    const { result } = view({
      permitted: false,
      comparison: { base: "branch-point", comparison: BRANCH_POINT, status: null },
    });
    expect(result.state).toMatchObject({ kind: "no-access", message: NO_ACCESS_MESSAGE });
  });
});

describe("Automatic's fallback and the last-commit override", () => {
  it("labels Automatic's last-commit fallback honestly and offers a retry", () => {
    const { result } = view({
      comparison: {
        base: "head",
        note: "Showing uncommitted changes: offline",
        override: false,
        retry: true,
      },
    });
    expect(result.trigger.base).toBe("HEAD");
    expect(result.fallback).toMatchObject({
      message: "Showing uncommitted changes: offline",
      canRetry: true,
      override: false,
    });
  });

  it("keeps Show uncommitted changes a session override that Compare again ends", () => {
    const { input, result } = view({
      intent: "branch-point",
      comparison: {
        base: "head",
        note: "Showing uncommitted changes.",
        override: true,
        retry: false,
      },
    });
    expect(result.fallback?.override).toBe(true);
    expect(result.reset.disabled).toBe(false);
    result.compareAgain();
    expect(input.actions.compareAgain).toHaveBeenCalledOnce();
    result.showLastCommit();
    expect(input.actions.showLastCommit).toHaveBeenCalledOnce();
  });
});

describe("an unknown baseline with a fallback or override", () => {
  // The owner's real notes, so a change to their wording is caught here.
  const offline = effectiveComparison({
    intent: "automatic",
    support: {
      kind: "unreachable",
      reason: "remote_host_offline",
      message: "This session's machine is offline.",
    },
    multipleRepositories: false,
    established: null,
    problem: null,
    connectTimedOut: false,
    headOverride: false,
  });
  const timedOut = effectiveComparison({
    intent: "automatic",
    support: { kind: "checking" },
    multipleRepositories: false,
    established: null,
    problem: null,
    connectTimedOut: true,
    headOverride: false,
  });
  const override = effectiveComparison({
    intent: "automatic",
    support: { kind: "available" },
    multipleRepositories: false,
    established: null,
    problem: null,
    connectTimedOut: false,
    headOverride: true,
  });

  it("never calls the host's changes last-commit changes", () => {
    expect(view({ headIsKnown: false, comparison: offline }).result.fallback?.message).toBe(
      `${SHOWING_HOST_CHANGES}: This session's machine is offline.`,
    );
    expect(view({ headIsKnown: false, comparison: timedOut }).result.fallback?.message).toBe(
      `${SHOWING_HOST_CHANGES}: ${COULD_NOT_REACH}`,
    );
    expect(view({ headIsKnown: false, comparison: override }).result.fallback?.message).toBe(
      `${SHOWING_HOST_CHANGES}.`,
    );
    for (const comparison of [offline, timedOut, override]) {
      const { result } = view({ headIsKnown: false, comparison });
      expect(result.fallback?.message).not.toMatch(/last.commit/i);
      expect(result.trigger.base).toBeNull();
    }
  });

  it("retries the branch point from a route or timeout fallback, never just HEAD's list", () => {
    for (const comparison of [offline, timedOut]) {
      const { input, result } = view({ comparison });
      expect(result.fallback?.canRetry).toBe(true);
      result.fallback?.retry();
      expect(input.actions.retryBranchPoint).toHaveBeenCalledOnce();
      expect(input.actions.refetchHead).not.toHaveBeenCalled();
    }
  });

  it("keeps the owner's wording when the workspace is known to be Git", () => {
    expect(view({ comparison: offline }).result.fallback?.message).toBe(
      "Showing uncommitted changes: This session's machine is offline.",
    );
  });
});

describe("presets, reset and retry", () => {
  it("offers the branch point and HEAD through the shared commands", () => {
    const { input, result } = view({
      intent: "head",
      commands: commands("head", "Update Omnigent to compare with the branch point."),
    });
    expect(result.presets.map(({ id, label, selected }) => ({ id, label, selected }))).toEqual([
      { id: "branch-point", label: "Default branch (merge base)", selected: false },
      { id: "head", label: "HEAD", selected: true },
    ]);
    expect(result.presets[0].unavailableReason).toBe(
      "Update Omnigent to compare with the branch point.",
    );
    result.presets[1].choose();
    expect(input.commands.find((command) => command.id === "compare-head")?.run).toHaveBeenCalled();
  });

  it("resets through the shared Automatic command, disabled while Automatic is chosen", () => {
    expect(view().result.reset.disabled).toBe(true);
    const { input, result } = view({ intent: "head", commands: commands("head") });
    expect(result.reset.disabled).toBe(false);
    result.reset.run();
    expect(
      input.commands.find((command) => command.id === "compare-automatic")?.run,
    ).toHaveBeenCalledOnce();
  });

  it("enables Reset under Automatic while a session override replaces it", () => {
    const { input, result } = view({
      intent: "automatic",
      comparison: {
        base: "head",
        note: "Showing uncommitted changes.",
        override: true,
        retry: false,
      },
    });
    expect(result.reset.disabled).toBe(false);
    result.reset.run();
    expect(
      input.commands.find((command) => command.id === "compare-automatic")?.run,
    ).toHaveBeenCalledOnce();
  });

  it("retries the source on screen: the runner for HEAD, the desktop app for the branch point", () => {
    // HEAD's list on screen: Retry asks the runner again.
    const head = view();
    head.result.retry();
    expect(head.input.actions.refetchHead).toHaveBeenCalledOnce();
    expect(head.input.actions.retryBranchPoint).not.toHaveBeenCalled();

    // The branch point on screen: Retry asks the desktop app again.
    const branchPoint = view({
      comparison: { base: "unavailable", message: "Comparing took too long.", retry: true },
    });
    branchPoint.result.retry();
    expect(branchPoint.input.actions.retryBranchPoint).toHaveBeenCalledOnce();
    expect(branchPoint.input.actions.refetchHead).not.toHaveBeenCalled();

    // Automatic fell back to HEAD: its note retries the branch point, Retry still HEAD's list.
    const fallback = view({
      comparison: {
        base: "head",
        note: "Showing uncommitted changes: couldn't reach this session's machine.",
        override: false,
        retry: true,
      },
    });
    expect(fallback.result.fallback?.canRetry).toBe(true);
    fallback.result.fallback?.retry();
    expect(fallback.input.actions.retryBranchPoint).toHaveBeenCalledOnce();
    expect(fallback.input.actions.refetchHead).not.toHaveBeenCalled();
    fallback.result.retry();
    expect(fallback.input.actions.refetchHead).toHaveBeenCalledOnce();
    expect(fallback.input.actions.retryBranchPoint).toHaveBeenCalledOnce();
  });
});

describe("why base and head can't be typed", () => {
  it.each([
    [{ kind: "unsupported", reason: "no_desktop", message: "Needs the app." }, "Needs the app."],
    [{ kind: "unreachable", reason: "remote_host_offline", message: "Offline." }, "Offline."],
    [{ kind: "checking" }, CHECKING_REVISIONS],
    [{ kind: "unsupported", reason: "disabled", message: "Turned off here." }, "Turned off here."],
  ] as [BranchPointSupport, string][])("says why for %o", (revisionSupport, note) => {
    const { result } = view({ revisionSupport });
    expect([result.revisions.editable, result.revisionsNote]).toEqual([false, note]);
  });

  it("lets them be typed where the session's machine compares revisions", () => {
    const { result } = view({ revisionSupport: AVAILABLE });
    expect([result.revisions.editable, result.revisionsNote]).toEqual([true, null]);
  });
});

describe("a typed base and head", () => {
  const V1 = { base: "v1", head: "" };

  it("names what was typed, checks no saved base, and Reset leaves it", () => {
    const { input, result } = view({
      custom: V1,
      revisionSupport: AVAILABLE,
      comparison: { base: "pending", message: "Comparing…" },
    });
    expect(result.trigger).toEqual({
      name: "Custom",
      base: "v1",
      head: "Working Copy",
      title: "Comparing…",
      invalid: false,
    });
    expect(result.presets.map((preset) => preset.selected)).toEqual([false, false]);
    expect(result.reset.disabled).toBe(false);
    result.reset.run();
    expect(
      input.commands.find((command) => command.id === "compare-automatic")?.run,
    ).toHaveBeenCalled();
    result.revisions.compare({ base: "v2", head: "" });
    expect(input.actions.compareRevisions).toHaveBeenCalledWith({ base: "v2", head: "" });
  });

  it("marks the trigger invalid, and points at the field the app named", () => {
    const { result } = view({
      custom: { base: "v1", head: "nope" },
      revisionSupport: AVAILABLE,
      comparison: {
        base: "unavailable",
        message: "Head “nope” wasn't found in this repository.",
        retry: false,
        field: "head",
      },
    });
    expect([result.trigger.base, result.trigger.head, result.trigger.invalid]).toEqual([
      "v1",
      "nope",
      true,
    ]);
    expect(result.revisions.fieldError).toEqual({
      field: "head",
      message: "Head “nope” wasn't found in this repository.",
    });
    expect(result.state).toMatchObject({ kind: "unavailable", title: COMPARISON_UNAVAILABLE });
  });

  it("names the default branch only from the branch point's own listing", () => {
    const typedListing = {
      ...BRANCH_POINT,
      ref: "v1",
      revisions: { spec: V1, baseDisplay: "v1", headDisplay: "", headIsWorkingCopy: true },
    };
    const { result } = view({
      custom: V1,
      revisionSupport: AVAILABLE,
      comparison: { base: "branch-point", comparison: typedListing, status: null },
    });
    expect(result.presets[0]?.label).toBe("Default branch (merge base)");
    expect(result.trigger.title).toBe("v1 compared with Working Copy");
  });
});

describe("the quick choices", () => {
  const SHOWN_BRANCH_POINT: EffectiveComparison = {
    base: "branch-point",
    comparison: BRANCH_POINT,
    status: null,
  };
  const marked = (result: ReturnType<typeof view>["result"]) =>
    result.quickChoices.filter((choice) => choice.active).map((choice) => choice.id);

  it("offers the three comparisons in order, described with the default ref", () => {
    const { result } = view({ comparison: SHOWN_BRANCH_POINT });
    expect(
      result.quickChoices.map(({ id, label, description }) => [id, label, description]),
    ).toEqual([
      ["compare-head", "Uncommitted changes", "Your working copy against the last commit"],
      [
        "compare-branch-point",
        "All branch changes",
        "Everything since this branch left origin/main",
      ],
      ["compare-last-commit", "Last commit", "What the last commit changed"],
    ]);
    // Without a branch point listed, nothing names a ref.
    expect(view().result.quickChoices[1]!.description).toBe(
      "Everything since this branch left the default branch",
    );
  });

  it("marks the comparison on screen, whichever choice led there", () => {
    // Automatic's branch point, and Automatic falling back to the last commit.
    expect(marked(view({ comparison: SHOWN_BRANCH_POINT }).result)).toEqual([
      "compare-branch-point",
    ]);
    const fallback: EffectiveComparison = {
      base: "head",
      note: "Showing uncommitted changes: Comparing took too long.",
      override: false,
      retry: true,
    };
    expect(marked(view({ comparison: fallback }).result)).toEqual(["compare-head"]);
    // Explicit choices.
    expect(marked(view({ intent: "head" }).result)).toEqual(["compare-head"]);
    expect(marked(view({ intent: "branch-point", comparison: SHOWN_BRANCH_POINT }).result)).toEqual(
      ["compare-branch-point"],
    );
  });

  it("marks Last commit only for HEAD~1 … HEAD, and names any other typed comparison Custom", () => {
    const typed = (custom: { base: string; head: string }) =>
      view({ custom, revisionSupport: AVAILABLE, comparison: SHOWN_BRANCH_POINT }).result;
    const lastCommit = typed({ base: "HEAD~1", head: "HEAD" });
    expect(marked(lastCommit)).toEqual(["compare-last-commit"]);
    expect(lastCommit.trigger).toMatchObject({
      name: "Last commit",
      title: "HEAD~1 compared with HEAD",
    });
    const other = typed({ base: "HEAD~2", head: "HEAD" });
    expect(marked(other)).toEqual([]);
    expect(other.trigger.name).toBe("Custom");
  });

  it("names a commit shown on its own by its short sha, and says what it changed", () => {
    const oid = "0123456789abcdef0123456789abcdef01234567";
    const { result } = view({
      custom: { base: `${oid}~1`, head: oid },
      revisionSupport: AVAILABLE,
      comparison: SHOWN_BRANCH_POINT,
    });
    expect(marked(result)).toEqual([]);
    expect(result.trigger).toMatchObject({
      name: "Commit 0123456",
      title: "What commit 0123456 changed",
    });
  });

  it("marks nothing while the requested comparison is pending or unavailable, and the trigger names it", () => {
    const pending = view({ comparison: { base: "pending", message: "Comparing…" } }).result;
    expect(marked(pending)).toEqual([]);
    expect(pending.trigger).toMatchObject({ name: "All branch changes", title: "Comparing…" });
    const unavailable = view({
      intent: "branch-point",
      comparison: {
        base: "unavailable",
        message: "No branch point with origin/main.",
        retry: false,
      },
    }).result;
    expect(marked(unavailable)).toEqual([]);
    expect(unavailable.trigger).toMatchObject({ name: "All branch changes", invalid: true });
    const typedPending = view({
      custom: { base: "HEAD~1", head: "HEAD" },
      revisionSupport: AVAILABLE,
      comparison: { base: "pending", message: "Comparing…" },
    }).result;
    expect(marked(typedPending)).toEqual([]);
    expect(typedPending.trigger.name).toBe("Last commit");
  });

  it("marks nothing and keeps today's trigger while the workspace isn't known to be Git", () => {
    const { result } = view({ headIsKnown: false });
    expect(marked(result)).toEqual([]);
    expect(result.trigger.name).toBeNull();
  });

  it("carries each shared command's reason, and choosing runs it even when marked", () => {
    const shared = view({ intent: "head" });
    const commandsWithReasons = shared.input.commands.map((entry) =>
      entry.id === "compare-last-commit"
        ? { ...entry, unavailableReason: CHECKING_REVISIONS }
        : entry,
    );
    const { input, result } = view({ intent: "head", commands: commandsWithReasons });
    expect(result.quickChoices.map((choice) => choice.unavailableReason)).toEqual([
      null,
      null,
      CHECKING_REVISIONS,
    ]);
    const uncommitted = result.quickChoices[0]!;
    expect(uncommitted.active).toBe(true);
    uncommitted.choose();
    expect(input.commands.find((entry) => entry.id === "compare-head")!.run).toHaveBeenCalledOnce();
  });
});

describe("the person's choice", () => {
  it("names the saved base, typed revisions and override, never the answer", () => {
    const automatic = view().result.choiceKey;
    expect(
      view({ comparison: { base: "branch-point", comparison: BRANCH_POINT, status: null } }).result
        .choiceKey,
    ).toBe(automatic);
    expect(view({ intent: "head" }).result.choiceKey).not.toBe(automatic);
    expect(view({ custom: { base: "v1", head: "" } }).result.choiceKey).not.toBe(automatic);
    expect(view({ comparison: { ...HEAD, override: true } }).result.choiceKey).not.toBe(automatic);
  });

  it("runs a hook before each comparison it offers, and not before refresh or retry", () => {
    const { input, result } = view({ revisionSupport: AVAILABLE });
    const order: string[] = [];
    const onChoose = vi.fn(() => order.push("chosen"));
    vi.mocked(input.actions.compareRevisions).mockImplementation(() => order.push("compared"));
    const chosen = onComparisonChosen(result, onChoose);
    chosen.revisions.compare({ base: "v1", head: "" });
    expect(order).toEqual(["chosen", "compared"]);
    for (const choice of chosen.quickChoices) choice.choose();
    for (const preset of chosen.presets) preset.choose();
    chosen.reset.run();
    chosen.showLastCommit();
    chosen.compareAgain();
    const offered = 1 + chosen.quickChoices.length + chosen.presets.length + 3;
    expect(onChoose).toHaveBeenCalledTimes(offered);
    chosen.refresh();
    chosen.retry();
    expect(onChoose).toHaveBeenCalledTimes(offered);
  });
});
