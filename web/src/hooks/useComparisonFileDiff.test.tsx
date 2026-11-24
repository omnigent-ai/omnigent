import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as WorkingCopyComparisonModule from "@/lib/workingCopyComparison";

const state = vi.hoisted(() => ({
  comparison: { base: "head", note: null, override: false, retry: false } as unknown,
  readable: true,
  permitted: true,
}));
vi.mock("@/hooks/useWorkingCopyChanges", () => ({
  useWorkingCopyChanges: () => ({
    comparison: state.comparison,
    generation: 4,
    permitted: state.permitted,
    readable: state.readable,
  }),
  useForgetDiffTextWhenRevoked: () => {},
}));
const reads = vi.hoisted(() => ({ fetchText: vi.fn() }));
vi.mock("@/lib/workingCopyComparison", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkingCopyComparisonModule>()),
  fetchComparisonFileText: (...args: unknown[]) => reads.fetchText(...args),
}));

import { ComparisonOwnerContext } from "@/hooks/useComparisonOwner";
import { comparisonFileDiffQueryKey } from "@/lib/branchPointFileDiff";
import { type ComparisonEntry, ComparisonFileError } from "@/lib/workingCopyComparison";
import { comparisonEntry } from "@/test/workingCopyComparisons";
import type { WorkingCopyComparisonOwner } from "@/lib/workingCopyComparisonOwner";

import { useComparisonFileDiff } from "./useComparisonFileDiff";

const SESSION = "conv_viewer";

/** Lets queries that would start, start. */
const settle = () =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, 0);
  });

function branchPoint(...entries: ComparisonEntry[]) {
  return {
    base: "branch-point",
    status: null,
    comparison: {
      id: "cmp_1",
      ref: "origin/main",
      baseOid: "b",
      headOid: "h",
      refOid: "r",
      entries,
      unrepresentableNames: 0,
      listedAt: 1,
    },
  };
}

const BASE = { comparisonId: "cmp_1", fingerprint: "fp-src/a.ts", generation: 4 };

function fakeOwner() {
  return {
    mayRead: vi.fn(() => true),
    readToken: vi.fn(() => 1),
    recover: vi.fn(async () => true),
    isCurrent: vi.fn(() => true),
    readSucceeded: vi.fn(),
  };
}

function renderDiff(path: string | null, owner = fakeOwner(), client = new QueryClient()) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <ComparisonOwnerContext.Provider value={owner as unknown as WorkingCopyComparisonOwner}>
        {children}
      </ComparisonOwnerContext.Provider>
    </QueryClientProvider>
  );
  return { ...renderHook(() => useComparisonFileDiff(SESSION, path), { wrapper }), owner, client };
}

beforeEach(() => {
  state.comparison = { base: "head", note: null, override: false, retry: false };
  state.readable = true;
  state.permitted = true;
  reads.fetchText.mockReset().mockResolvedValue({ before: "base", after: "work" });
});

describe("useComparisonFileDiff", () => {
  it("leaves the last commit's diff to the viewer, reading nothing", () => {
    const { result } = renderDiff("src/a.ts");
    expect(result.current).toEqual({ base: "head" });
    expect(reads.fetchText).not.toHaveBeenCalled();
  });

  it("reads a listed file under the comparison, into the Changes review's cache entry", async () => {
    state.comparison = branchPoint(comparisonEntry("src/a.ts"));
    const { result, client } = renderDiff("src/a.ts");
    const shown = result.current;
    expect(shown).toMatchObject({
      base: "comparison",
      changed: true,
      deleted: false,
      headIsWorkingCopy: true,
    });
    await waitFor(() =>
      expect(result.current.base === "comparison" && result.current.query.data).toMatchObject({
        before: "base",
        after: "work",
      }),
    );
    expect(reads.fetchText).toHaveBeenCalledWith(SESSION, "cmp_1", "src/a.ts", expect.anything());
    expect(client.getQueryData(comparisonFileDiffQueryKey(SESSION, "src/a.ts", BASE))).toEqual(
      expect.objectContaining({ before: "base", after: "work" }),
    );
  });

  it("shows the text the Changes review already read for this listing, without reading again", async () => {
    state.comparison = branchPoint(comparisonEntry("src/a.ts"));
    const client = new QueryClient();
    client.setQueryData(comparisonFileDiffQueryKey(SESSION, "src/a.ts", BASE), {
      object: "session.environment.filesystem.file_diff",
      path: "src/a.ts",
      before: "reviewed base",
      after: "reviewed work",
    });
    const { result } = renderDiff("src/a.ts", fakeOwner(), client);
    expect(result.current.base === "comparison" && result.current.query.data?.after).toBe(
      "reviewed work",
    );
    await settle();
    expect(reads.fetchText).not.toHaveBeenCalled();
  });

  it("lists again through the comparison owner when the listing went stale, then reads again", async () => {
    state.comparison = branchPoint(comparisonEntry("src/a.ts"));
    reads.fetchText
      .mockRejectedValueOnce(new ComparisonFileError("comparison_expired"))
      .mockResolvedValueOnce({ before: "base", after: "relisted" });
    const { result, owner } = renderDiff("src/a.ts");
    await waitFor(() =>
      expect(result.current.base === "comparison" && result.current.query.data?.after).toBe(
        "relisted",
      ),
    );
    expect(owner.recover).toHaveBeenCalledTimes(1);
    expect(owner.recover).toHaveBeenCalledWith(SESSION, "cmp_1", "src/a.ts", 1);
  });

  it("says when a typed comparison's head is a commit, not the working copy", () => {
    const committed = branchPoint(comparisonEntry("src/a.ts"));
    (committed.comparison as Record<string, unknown>).revisions = {
      spec: { base: "main", head: "feature" },
      baseDisplay: "main",
      headDisplay: "feature",
      headIsWorkingCopy: false,
    };
    state.comparison = committed;
    expect(renderDiff("src/a.ts").result.current).toMatchObject({ headIsWorkingCopy: false });
  });

  it("has no diff for a file the comparison doesn't list, and names a deleted one", () => {
    state.comparison = branchPoint(comparisonEntry("src/gone.ts", { status: "deleted" }));
    expect(renderDiff("src/a.ts").result.current).toMatchObject({
      base: "comparison",
      changed: false,
      deleted: false,
    });
    expect(renderDiff("src/gone.ts").result.current).toMatchObject({
      changed: true,
      deleted: true,
    });
    expect(reads.fetchText).toHaveBeenCalledTimes(1);
  });

  it.each([
    [
      "the comparison is being decided",
      () => (state.comparison = { base: "pending", message: "" }),
    ],
    ["the workspace can't be read now", () => (state.readable = false)],
    ["the session's files aren't shared", () => (state.permitted = false)],
  ])("reads nothing while %s", async (_name, arrange) => {
    state.comparison = branchPoint(comparisonEntry("src/a.ts"));
    arrange();
    const { result } = renderDiff("src/a.ts");
    expect(result.current.base).toBe("comparison");
    await settle();
    expect(reads.fetchText).not.toHaveBeenCalled();
  });
});
