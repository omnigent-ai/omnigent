import type * as UseSessionModule from "@/hooks/useSession";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const workspace = vi.hoisted(() => ({
  head: {
    data: undefined as unknown,
    isError: false,
    error: null,
    isSuccess: false,
    dataUpdatedAt: 0,
  },
  environmentAvailable: true,
  serveable: true as boolean | undefined,
}));

const sessionAccess = vi.hoisted(() => ({
  session: null as { permissionLevel: number | null; shareWorkspaceFiles?: boolean } | null,
}));
vi.mock("@/hooks/useSession", async (importOriginal) => ({
  ...(await importOriginal<typeof UseSessionModule>()),
  useSession: () => ({ session: sessionAccess.session, isLoading: false, error: null }),
}));

vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useWorkspaceChangedFiles: () => workspace.head,
  useWorkspaceEnvironment: () => ({ data: { available: workspace.environmentAvailable } }),
  useWorkspaceServeable: () => workspace.serveable,
}));

import { LEVEL_READ } from "@/lib/permissionsApi";
import { setSessionHost } from "@/lib/sessionHost";
import type { BranchPointListing } from "@/lib/workingCopyComparison";
import {
  writeWorkingCopyBaseIntent,
  readWorkingCopyBaseIntent,
  subscribeWorkingCopyBaseIntent,
} from "@/lib/workingCopyBasePreference";
import { WorkingCopyComparisonOwner } from "@/lib/workingCopyComparisonOwner";
import {
  invalidateWorkingCopyChanges,
  setWorkingCopyComparisonsForTests,
  sessionChoiceCount,
  workingCopyComparisonsAt,
  workspaceKeyOf,
} from "@/lib/workingCopyComparisons";

import { ComparisonOwnerContext } from "./useComparisonOwner";
import { useWorkingCopyBaseCommands, useWorkingCopyChanges } from "./useWorkingCopyChanges";

const SESSION = "conv_hook";

function file(path: string, extra: Record<string, unknown> = {}) {
  return {
    path,
    name: path,
    status: "modified" as const,
    bytes: 1,
    modified_at: null,
    lines_added: 1,
    lines_removed: 0,
    ...extra,
  };
}

function branchPointEntry(path: string) {
  return {
    ...file(path),
    change: null,
    baseKind: "file" as const,
    workingKind: "file" as const,
    binary: false,
    problem: null,
    fingerprint: `fp-${path}`,
  };
}

let listings: { resolve: (listing: BranchPointListing) => void; signal: AbortSignal }[];

function installOwner() {
  listings = [];
  const owner = new WorkingCopyComparisonOwner({
    fetchListing: (_sessionId, { signal }) =>
      new Promise((resolve) => {
        listings.push({ resolve, signal });
      }),
    watchRoute: (_target, onRoute) => {
      queueMicrotask(() => onRoute({ state: "served" }));
      return { stop: () => {}, ask: () => {} };
    },
    readIntent: readWorkingCopyBaseIntent,
    subscribeIntent: subscribeWorkingCopyBaseIntent,
    now: () => Date.now(),
    setTimeout: (callback, ms) => setTimeout(callback, ms),
    clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
    documentVisible: () => true,
    subscribeAttention: () => () => {},
  });
  setWorkingCopyComparisonsForTests(owner);
  return owner;
}

function answerListing(paths: string[], id = "cmp_1") {
  const pending = listings.at(-1);
  expect(pending).toBeDefined();
  act(() => {
    pending!.resolve({
      kind: "comparison",
      comparison: {
        id,
        ref: "origin/main",
        baseOid: "b",
        headOid: "h",
        refOid: "r",
        entries: paths.map(branchPointEntry),
        unrepresentableNames: 0,
        listedAt: 123,
      },
    });
  });
}

beforeEach(() => {
  workspace.head = {
    data: { available: true, data: [file("edited.ts")] },
    isError: false,
    error: null,
    isSuccess: true,
    dataUpdatedAt: 1,
  };
  workspace.environmentAvailable = true;
  workspace.serveable = true;
  // A loaded session with permissions off, as in single-user mode.
  sessionAccess.session = { permissionLevel: null };
  installOwner();
  setSessionHost(SESSION, "host_1");
});

afterEach(() => {
  setSessionHost(SESSION, null);
  setWorkingCopyComparisonsForTests(null);
  window.localStorage.clear();
});

describe("useWorkingCopyChanges", () => {
  it("shows nothing while the first comparison is coming, then the branch point's files", async () => {
    const { result } = renderHook(() => useWorkingCopyChanges(SESSION, { kind: "surface" }));
    expect(result.current.comparison.base).toBe("pending");
    expect(result.current.data).toBeUndefined();
    await waitFor(() => expect(listings).toHaveLength(1));
    answerListing(["committed-on-branch.ts", "edited.ts"]);
    await waitFor(() => expect(result.current.comparison.base).toBe("branch-point"));
    expect(result.current.data?.data.map((entry) => entry.path)).toEqual([
      "committed-on-branch.ts",
      "edited.ts",
    ]);
    expect(result.current.dataUpdatedAt).toBe(123);
  });

  it("shows last-commit changes, without the branch's commits, once the person chooses them", async () => {
    const { result } = renderHook(() => useWorkingCopyChanges(SESSION, { kind: "count" }));
    await waitFor(() => expect(listings).toHaveLength(1));
    answerListing(["committed-on-branch.ts", "edited.ts"]);
    await waitFor(() => expect(result.current.comparison.base).toBe("branch-point"));
    act(() => {
      writeWorkingCopyBaseIntent("head");
    });
    expect(result.current.comparison).toMatchObject({ base: "head", note: null });
    expect(result.current.data?.data.map((entry) => entry.path)).toEqual(["edited.ts"]);
  });

  it("gives every view of a session the same comparison from one listing", async () => {
    const { result } = renderHook(() => ({
      badge: useWorkingCopyChanges(SESSION, { kind: "count" }),
      list: useWorkingCopyChanges(SESSION, { kind: "surface" }),
      viewer: useWorkingCopyChanges(SESSION, { kind: "file" }),
    }));
    await waitFor(() => expect(listings).toHaveLength(1));
    answerListing(["committed-on-branch.ts"]);
    await waitFor(() => expect(result.current.viewer.comparison.base).toBe("branch-point"));
    expect(result.current.badge.data?.data).toBe(result.current.list.data?.data);
    expect(result.current.viewer.data?.data).toBe(result.current.list.data?.data);
    expect(listings).toHaveLength(1);
  });

  it("uses the last commit when the workspace spans several repositories", async () => {
    workspace.head = {
      ...workspace.head,
      data: { available: true, data: [file("a/x.ts", { repository_root: "a" })] },
    };
    const { result } = renderHook(() => useWorkingCopyChanges(SESSION, { kind: "surface" }));
    await waitFor(() => expect(listings).toHaveLength(1));
    answerListing(["x.ts"]);
    await waitFor(() =>
      expect(result.current.comparison).toMatchObject({
        base: "head",
        note: "Branch point works for a single Git repository.",
      }),
    );
  });

  it("reads nothing until the session's access is known, then lists for a viewer with files", async () => {
    sessionAccess.session = null;
    const { rerender } = renderHook(() => useWorkingCopyChanges(SESSION, { kind: "surface" }));
    await act(async () => {
      await Promise.resolve();
    });
    expect(listings).toHaveLength(0);
    sessionAccess.session = { permissionLevel: LEVEL_READ, shareWorkspaceFiles: true };
    rerender();
    await waitFor(() => expect(listings).toHaveLength(1));
  });

  it("lists nothing, and shows nothing, while the workspace can't be read", async () => {
    workspace.environmentAvailable = false;
    const { result, rerender } = renderHook(() =>
      useWorkingCopyChanges(SESSION, { kind: "surface" }),
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(listings).toHaveLength(0);
    workspace.environmentAvailable = true;
    rerender();
    await waitFor(() => expect(listings).toHaveLength(1));
    // Losing access mid-list abandons the list.
    workspace.serveable = false;
    rerender();
    expect(listings[0]!.signal.aborted).toBe(true);
    expect(result.current.data).toBeUndefined();
  });

  it("lists the branch point again on invalidation, with no last-commit query in the cache", async () => {
    const queryClient = new QueryClient();
    renderHook(() => useWorkingCopyChanges(SESSION, { kind: "surface" }));
    await waitFor(() => expect(listings).toHaveLength(1));
    answerListing(["a.ts"]);
    expect(queryClient.getQueryCache().findAll({ queryKey: ["workspace-changed-files"] })).toEqual(
      [],
    );
    invalidateWorkingCopyChanges(queryClient, SESSION);
    await waitFor(() => expect(listings).toHaveLength(2));
  });
});

describe("a base chosen in a repository folder's review", () => {
  it("keeps the workspace's saved base, comparison and count", async () => {
    const client = new QueryClient();
    const plain = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const folder = workingCopyComparisonsAt(".worktrees/feature");
    const inFolder = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>
        <ComparisonOwnerContext.Provider value={folder}>{children}</ComparisonOwnerContext.Provider>
      </QueryClientProvider>
    );
    // The sidebar's count: Automatic shows the workspace's branch point.
    const count = renderHook(() => useWorkingCopyChanges(SESSION, { kind: "count" }), {
      wrapper: plain,
    });
    await waitFor(() => expect(listings).toHaveLength(1));
    answerListing(["branch_only.ts"]);
    await waitFor(() => expect(count.result.current.comparison.base).toBe("branch-point"));

    const folderCommands = renderHook(() => useWorkingCopyBaseCommands(SESSION), {
      wrapper: inFolder,
    });
    act(() =>
      folderCommands.result.current.find((command) => command.id === "compare-head")!.run(),
    );

    const checked = (commands: { id: string; checked?: boolean }[]) =>
      commands.find((command) => command.checked)?.id;
    expect(checked(folderCommands.result.current)).toBe("compare-head");
    expect(readWorkingCopyBaseIntent()).toBe("automatic");
    const workspaceCommands = renderHook(() => useWorkingCopyBaseCommands(SESSION), {
      wrapper: plain,
    });
    expect(checked(workspaceCommands.result.current)).toBe("compare-automatic");
    expect(count.result.current.comparison.base).toBe("branch-point");
    expect(count.result.current.data?.data.map((entry) => entry.path)).toEqual(["branch_only.ts"]);
  });

  it("stays apart from a folder of the same name in another workspace or host", () => {
    const client = new QueryClient();
    const inFolderAt = (owner: WorkingCopyComparisonOwner) =>
      function InFolder({ children }: { children: ReactNode }) {
        return (
          <QueryClientProvider client={client}>
            <ComparisonOwnerContext.Provider value={owner}>
              {children}
            </ComparisonOwnerContext.Provider>
          </QueryClientProvider>
        );
      };
    const at = (hostId: string, root: string) =>
      workingCopyComparisonsAt(".worktrees/feature", workspaceKeyOf(hostId, root));
    const alpha = at("host-alpha", "/projects/alpha");
    const beta = at("host-beta", "/projects/beta");
    const sameHostOtherRoot = at("host-alpha", "/projects/other");
    const render = (owner: WorkingCopyComparisonOwner) =>
      renderHook(() => useWorkingCopyBaseCommands(SESSION), { wrapper: inFolderAt(owner) });
    const checked = (commands: { id: string; checked?: boolean }[]) =>
      commands.find((command) => command.checked)?.id;
    const inAlpha = render(alpha);
    const inBeta = render(beta);
    const inOther = render(sameHostOtherRoot);

    act(() => inAlpha.result.current.find((command) => command.id === "compare-head")!.run());

    expect(checked(inAlpha.result.current)).toBe("compare-head");
    expect(checked(inBeta.result.current)).toBe("compare-automatic");
    expect(checked(inOther.result.current)).toBe("compare-automatic");
  });

  it("counts a choice there as the session's, so a tab's landing knows it is older", () => {
    const client = new QueryClient();
    const folder = workingCopyComparisonsAt(".worktrees/feature");
    const inFolder = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>
        <ComparisonOwnerContext.Provider value={folder}>{children}</ComparisonOwnerContext.Provider>
      </QueryClientProvider>
    );
    const commands = renderHook(() => useWorkingCopyBaseCommands(SESSION), { wrapper: inFolder });
    const before = sessionChoiceCount(SESSION);
    act(() => commands.result.current.find((command) => command.id === "compare-head")!.run());
    expect(sessionChoiceCount(SESSION)).toBe(before + 1);
  });

  it("follows the workspace's saved base until a base is chosen there", () => {
    const client = new QueryClient();
    const folder = workingCopyComparisonsAt(".worktrees/feature");
    const inFolder = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>
        <ComparisonOwnerContext.Provider value={folder}>{children}</ComparisonOwnerContext.Provider>
      </QueryClientProvider>
    );
    const commands = renderHook(() => useWorkingCopyBaseCommands(SESSION), { wrapper: inFolder });
    const checked = () => commands.result.current.find((command) => command.checked)?.id;
    expect(checked()).toBe("compare-automatic");
    act(() => void writeWorkingCopyBaseIntent("branch-point"));
    expect(checked()).toBe("compare-branch-point");
  });
});
