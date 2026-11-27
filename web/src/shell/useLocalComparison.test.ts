import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { WorkingCopyChanges } from "@/hooks/useWorkingCopyChanges";
import { useLocalComparison } from "./useLocalComparison";

const mocks = vi.hoisted(() => ({
  changes: vi.fn(),
  base: vi.fn(),
  retry: vi.fn(),
  showLastCommit: vi.fn(),
  compareAgain: vi.fn(),
  compareRevisions: vi.fn(),
  commands: vi.fn(() => [] as unknown[]),
}));

vi.mock("@/hooks/useWorkingCopyChanges", () => ({
  useWorkingCopyChanges: mocks.changes,
  useWorkingCopyBaseCommands: () => mocks.commands(),
  workingCopyComparisonActions: () => ({
    retry: mocks.retry,
    showLastCommit: mocks.showLastCommit,
    compareAgain: mocks.compareAgain,
    compareRevisions: mocks.compareRevisions,
  }),
}));
vi.mock("./workingCopyBase", () => ({ useWorkingCopyBase: mocks.base }));

function changes(overrides: Partial<WorkingCopyChanges> = {}): WorkingCopyChanges {
  return {
    data: { available: true, data: [] },
    isError: false,
    error: null,
    isSuccess: true,
    isLoading: false,
    dataUpdatedAt: 1,
    comparison: { base: "head", note: null, override: false, retry: false },
    support: { kind: "available" },
    revisionSupport: { kind: "unsupported", reason: "update_app", message: "Update" },
    custom: null,
    workspace: null,
    refetch: vi.fn(() => Promise.resolve()),
    generation: 3,
    permitted: true,
    accessEpoch: 0,
    choices: 0,
    readable: true,
    ...overrides,
  };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("useLocalComparison", () => {
  it("keeps the comparison current only while the review is on screen", () => {
    mocks.changes.mockReturnValue(changes());
    mocks.base.mockReturnValue(null);
    const { rerender } = renderHook(({ active }) => useLocalComparison("s1", { active }), {
      initialProps: { active: true },
    });
    expect(mocks.changes).toHaveBeenLastCalledWith("s1", { kind: "surface" });
    rerender({ active: false });
    expect(mocks.changes).toHaveBeenLastCalledWith("s1", { kind: "file" });
  });

  it("returns the shared changes as they are, for the review's read guards", () => {
    const shared = changes();
    mocks.changes.mockReturnValue(shared);
    mocks.base.mockReturnValue(null);
    const { result } = renderHook(() => useLocalComparison("s1", { active: true }));
    expect(result.current.changes).toBe(shared);
  });

  it("names HEAD only when the worktree probe found Git", () => {
    mocks.changes.mockReturnValue(changes());
    mocks.base.mockReturnValue(null);
    const unknown = renderHook(() => useLocalComparison("s1", { active: true }));
    expect(unknown.result.current.view.trigger.base).toBeNull();
    mocks.base.mockReturnValue({ label: "vs HEAD", description: "" });
    const known = renderHook(() => useLocalComparison("s1", { active: true }));
    expect(known.result.current.view.trigger.base).toBe("HEAD");
  });

  it("retries the runner's list for HEAD and the desktop app for the branch point", () => {
    const head = changes();
    mocks.changes.mockReturnValue(head);
    mocks.base.mockReturnValue(null);
    renderHook(() => useLocalComparison("s1", { active: true })).result.current.view.retry();
    expect(head.refetch).toHaveBeenCalledOnce();
    expect(mocks.retry).not.toHaveBeenCalled();

    mocks.changes.mockReturnValue(
      changes({
        comparison: { base: "unavailable", message: "Comparing took too long.", retry: true },
      }),
    );
    renderHook(() => useLocalComparison("s1", { active: true })).result.current.view.retry();
    expect(mocks.retry).toHaveBeenCalledOnce();
  });

  it("applies a comparison through the menu's own commands, only where the menu would", () => {
    const run = vi.fn();
    mocks.commands.mockReturnValue([
      { id: "compare-head", label: "", name: "", unavailableReason: null, run },
      { id: "compare-branch-point", label: "", name: "", unavailableReason: "No", run: vi.fn() },
    ]);
    mocks.base.mockReturnValue(null);
    mocks.changes.mockReturnValue(changes({ revisionSupport: { kind: "checking" } }));
    const { choice } = renderHook(() => useLocalComparison("s1", { active: true })).result.current;
    const head = { kind: "saved", intent: "head" } as const;
    const typed = { kind: "typed", base: "v1", head: "" } as const;
    expect(choice.availability(head)).toBe("now");
    expect(choice.availability({ kind: "saved", intent: "branch-point" })).toEqual({
      unavailable: "No",
    });
    expect(choice.availability(typed)).toBe("later");
    choice.choose(head);
    expect(run).toHaveBeenCalledOnce();
    choice.choose(typed);
    expect(mocks.compareRevisions).toHaveBeenCalledWith({ base: "v1", head: "" });
  });
});
