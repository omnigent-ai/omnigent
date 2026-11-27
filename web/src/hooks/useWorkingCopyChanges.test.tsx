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
  environmentRoot: undefined as string | undefined,
  serveable: true as boolean | undefined,
  // Whether the last render asked the runner for its listing.
  runnerEnabled: undefined as boolean | undefined,
}));

const sessionAccess = vi.hoisted(() => ({
  session: null as { permissionLevel: number | null; shareWorkspaceFiles?: boolean } | null,
}));
vi.mock("@/hooks/useSession", async (importOriginal) => ({
  ...(await importOriginal<typeof UseSessionModule>()),
  useSession: () => ({ session: sessionAccess.session, isLoading: false, error: null }),
}));

vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useWorkspaceChangedFiles: (_sessionId: string | undefined, options?: { enabled?: boolean }) => {
    workspace.runnerEnabled = options?.enabled;
    return workspace.head;
  },
  useWorkspaceEnvironment: () => ({
    data: { available: workspace.environmentAvailable, root: workspace.environmentRoot },
  }),
  useWorkspaceServeable: () => workspace.serveable,
}));

import { LEVEL_READ } from "@/lib/permissionsApi";
import { setSessionHost } from "@/lib/sessionHost";
import type { BranchPointListing } from "@/lib/workingCopyComparison";
import type { RevisionSpec } from "@/lib/customComparison";
import {
  baseIntentSource,
  resetFolderBaseIntentsForTests,
  writeWorkingCopyBaseIntent,
  readWorkingCopyBaseIntent,
} from "@/lib/workingCopyBasePreference";
import { LAST_COMMIT_SPEC, WorkingCopyComparisonOwner } from "@/lib/workingCopyComparisonOwner";
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

let listings: {
  resolve: (listing: BranchPointListing) => void;
  signal: AbortSignal;
  spec: RevisionSpec | null;
}[];

/** An owner whose listings the test answers: the workspace's, or a repository folder's. */
function makeOwner(folder?: { repository: string; workspaceKey: string }) {
  const intent = baseIntentSource(folder?.repository ?? "", folder?.workspaceKey);
  return new WorkingCopyComparisonOwner({
    ...folder,
    fetchListing: (_sessionId, { signal, spec }) =>
      new Promise((resolve) => {
        listings.push({ resolve, signal, spec });
      }),
    watchRoute: (_target, onRoute) => {
      queueMicrotask(() => onRoute({ state: "served" }));
      return { stop: () => {}, ask: () => {} };
    },
    readIntent: intent.read,
    subscribeIntent: intent.subscribe,
    now: () => Date.now(),
    setTimeout: (callback, ms) => setTimeout(callback, ms),
    clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
    documentVisible: () => true,
    subscribeAttention: () => () => {},
  });
}

function installOwner() {
  listings = [];
  const owner = makeOwner();
  setWorkingCopyComparisonsForTests(owner);
  return owner;
}

/** Renders under `client`, and in `owner`'s repository folder review when given. */
function wrapperFor(client: QueryClient, owner?: WorkingCopyComparisonOwner) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        {owner ? (
          <ComparisonOwnerContext.Provider value={owner}>
            {children}
          </ComparisonOwnerContext.Provider>
        ) : (
          children
        )}
      </QueryClientProvider>
    );
  };
}

/** The base command shown as chosen. */
function checkedId(commands: { id: string; checked?: boolean }[]) {
  return commands.find((command) => command.checked)?.id;
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
        // The host names the revisions it compared when they were asked for.
        ...(pending!.spec && {
          revisions: {
            spec: pending!.spec,
            baseDisplay: "HEAD",
            headDisplay: "Working copy",
            headIsWorkingCopy: true,
          },
        }),
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
  workspace.environmentRoot = undefined;
  workspace.serveable = true;
  // A loaded session with permissions off, as in single-user mode.
  sessionAccess.session = { permissionLevel: null };
  installOwner();
  setSessionHost(SESSION, "host_1");
});

afterEach(() => {
  setSessionHost(SESSION, null);
  setWorkingCopyComparisonsForTests(null);
  resetFolderBaseIntentsForTests();
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

describe("a repository folder's review", () => {
  const FOLDER = {
    repository: ".worktrees/feature",
    workspaceKey: workspaceKeyOf("host_1", "/repo"),
  };

  it("shows the host's last-commit listing, never the runner's workspace files", async () => {
    // A folder's last commit is a typed comparison in its workspace, made once the root is known.
    workspace.environmentRoot = "/repo";
    const owner = makeOwner(FOLDER);
    baseIntentSource(FOLDER.repository, FOLDER.workspaceKey).write("head");
    const { result } = renderHook(() => useWorkingCopyChanges(SESSION, { kind: "surface" }), {
      wrapper: wrapperFor(new QueryClient(), owner),
    });
    expect(workspace.runnerEnabled).toBe(false);
    expect(result.current.data).toBeUndefined();
    await waitFor(() => expect(listings).toHaveLength(1));
    expect(listings[0]!.spec).toEqual(LAST_COMMIT_SPEC);
    expect(result.current.data).toBeUndefined();

    answerListing(["feature_only.ts"]);
    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(result.current.comparison.base).toBe("head");
    expect(result.current.data?.data.map((entry) => entry.path)).toEqual(["feature_only.ts"]);
    expect(result.current.dataUpdatedAt).toBe(123);
    expect(workspace.runnerEnabled).toBe(false);
    owner.dispose();
  });

  it("keeps the workspace's saved base, comparison and count when a base is chosen there", async () => {
    const client = new QueryClient();
    // The sidebar's count: Automatic shows the workspace's branch point.
    const count = renderHook(() => useWorkingCopyChanges(SESSION, { kind: "count" }), {
      wrapper: wrapperFor(client),
    });
    await waitFor(() => expect(listings).toHaveLength(1));
    answerListing(["branch_only.ts"]);
    await waitFor(() => expect(count.result.current.comparison.base).toBe("branch-point"));

    const folder = workingCopyComparisonsAt(".worktrees/feature");
    const folderCommands = renderHook(() => useWorkingCopyBaseCommands(SESSION), {
      wrapper: wrapperFor(client, folder),
    });
    act(() =>
      folderCommands.result.current.find((command) => command.id === "compare-head")!.run(),
    );

    expect(checkedId(folderCommands.result.current)).toBe("compare-head");
    expect(readWorkingCopyBaseIntent()).toBe("automatic");
    const workspaceCommands = renderHook(() => useWorkingCopyBaseCommands(SESSION), {
      wrapper: wrapperFor(client),
    });
    expect(checkedId(workspaceCommands.result.current)).toBe("compare-automatic");
    expect(count.result.current.comparison.base).toBe("branch-point");
    expect(count.result.current.data?.data.map((entry) => entry.path)).toEqual(["branch_only.ts"]);
  });

  it("keeps a base chosen there apart from a same-named folder in another workspace or host", () => {
    const client = new QueryClient();
    const at = (hostId: string, root: string) =>
      workingCopyComparisonsAt(".worktrees/feature", workspaceKeyOf(hostId, root));
    const render = (owner: WorkingCopyComparisonOwner) =>
      renderHook(() => useWorkingCopyBaseCommands(SESSION), { wrapper: wrapperFor(client, owner) });
    const inAlpha = render(at("host-alpha", "/projects/alpha"));
    const inBeta = render(at("host-beta", "/projects/beta"));
    const inOther = render(at("host-alpha", "/projects/other"));

    act(() => inAlpha.result.current.find((command) => command.id === "compare-head")!.run());

    expect(checkedId(inAlpha.result.current)).toBe("compare-head");
    expect(checkedId(inBeta.result.current)).toBe("compare-automatic");
    expect(checkedId(inOther.result.current)).toBe("compare-automatic");
  });

  it("counts a choice there as the session's, so a tab's landing knows it is older", () => {
    const folder = workingCopyComparisonsAt(".worktrees/feature");
    const commands = renderHook(() => useWorkingCopyBaseCommands(SESSION), {
      wrapper: wrapperFor(new QueryClient(), folder),
    });
    const before = sessionChoiceCount(SESSION);
    act(() => commands.result.current.find((command) => command.id === "compare-head")!.run());
    expect(sessionChoiceCount(SESSION)).toBe(before + 1);
  });

  it("follows the workspace's saved base until a base is chosen there", () => {
    const folder = workingCopyComparisonsAt(".worktrees/feature");
    const commands = renderHook(() => useWorkingCopyBaseCommands(SESSION), {
      wrapper: wrapperFor(new QueryClient(), folder),
    });
    expect(checkedId(commands.result.current)).toBe("compare-automatic");
    act(() => void writeWorkingCopyBaseIntent("branch-point"));
    expect(checkedId(commands.result.current)).toBe("compare-branch-point");
  });
});
