import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type * as BranchCommitsModule from "@/lib/branchCommits";
import { BRANCH_RANGE, fetchBranchCommits } from "@/lib/branchCommits";
import { commitRevisions, type RevisionSpec } from "@/lib/customComparison";

import { useBranchCommits } from "./useBranchCommits";

vi.mock("@/lib/branchCommits", async (importOriginal) => ({
  ...(await importOriginal<typeof BranchCommitsModule>()),
  fetchBranchCommits: vi.fn(async () => ({
    kind: "listed",
    commits: [],
    truncated: false,
    baseDisplay: "v1",
  })),
}));

const WORKSPACE = { hostId: "host_1", root: "/repo" };
const TYPED = { base: "v1", head: "feature" };

afterEach(() => vi.mocked(fetchBranchCommits).mockClear());

function renderCommits(initialCustom: RevisionSpec | null) {
  const client = new QueryClient();
  return renderHook(
    ({
      custom,
      sessionId = "s1",
      workspace = WORKSPACE,
    }: {
      custom: RevisionSpec | null;
      sessionId?: string;
      workspace?: typeof WORKSPACE;
    }) => useBranchCommits(sessionId, workspace, custom, { enabled: true, shown: true }),
    {
      initialProps: { custom: initialCustom } as {
        custom: RevisionSpec | null;
        sessionId?: string;
        workspace?: typeof WORKSPACE;
      },
      wrapper: ({ children }: { children: ReactNode }) =>
        createElement(QueryClientProvider, { client }, children),
    },
  );
}

const ranges = () => vi.mocked(fetchBranchCommits).mock.calls.map(([, range]) => range);

describe("useBranchCommits", () => {
  it("lists the comparison's range, and keeps it while one commit is shown on its own", async () => {
    const { rerender } = renderCommits(TYPED);
    await waitFor(() => expect(ranges()).toEqual([TYPED]));
    rerender({ custom: commitRevisions("a".repeat(40)) });
    rerender({ custom: commitRevisions("b".repeat(40)) });
    // The list stays put, so nothing is listed afresh.
    expect(ranges()).toEqual([TYPED]);

    rerender({ custom: null });
    await waitFor(() => expect(ranges()).toEqual([TYPED, BRANCH_RANGE]));
  });

  it.each([
    ["session", { sessionId: "s2" }],
    // The host and the root each name the workspace on their own.
    ["host", { workspace: { ...WORKSPACE, hostId: "host_2" } }],
    ["workspace root", { workspace: { ...WORKSPACE, root: "/other" } }],
  ])("lists the branch, not the last range, for one commit in another %s", async (_name, next) => {
    const { rerender } = renderCommits(TYPED);
    await waitFor(() => expect(ranges()).toEqual([TYPED]));

    rerender({ custom: commitRevisions("a".repeat(40)), ...next });

    await waitFor(() => expect(ranges()).toEqual([TYPED, BRANCH_RANGE]));
  });
});
