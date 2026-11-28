// Close and Reopen on a Pull Requests list row: where the session's host serves the state change,
// the row offers it, sends it through the host once and toasts the outcome with an Undo that
// sends the inverse; where the host doesn't serve it, the row offers neither.

import type * as SonnerModule from "sonner";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const host = vi.hoisted(() => ({
  served: new Set<string>(),
  write: vi.fn(),
  toast: vi.fn(),
}));
vi.mock("sonner", async (importOriginal) => ({
  ...(await importOriginal<typeof SonnerModule>()),
  toast: Object.assign(host.toast, { success: vi.fn(), error: vi.fn(), dismiss: vi.fn() }),
}));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => ({ features: {} }) }));
vi.mock("@/lib/workspaceApi", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  enabledWorkspaceRoutes: () => ["server"],
  servingRoutes: async () => ({
    serving: [{ route: "server", key: "server:session-1", served: host.served }],
    failures: [],
  }),
}));
vi.mock("@/lib/pullRequestRpc", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  writePullRequestRpc: host.write,
}));

import type { WorkspaceClient } from "@/lib/workspaceApi";
import { resetLifecycleActionsForTests } from "@/pullRequestPage/pullRequestLifecycleActions";
import {
  resetPullRequestPageRoutesForTests,
  usePullRequestPageRoute,
} from "@/pullRequestPage/pullRequestPageRoute";
import { resetStateChainsForTests } from "@/pullRequestPage/pullRequestStateChain";

import type { PullRequestListEntry } from "./PullRequestListRows";
import { useRowStateChanges } from "./useRowStateChanges";

const PR = "https://github.com/acme/app/pull/42";
const SESSION = "session-1";
const READS = [
  "GetPullRequest",
  "ListPullRequestFiles",
  "GetPullRequestDiff",
  "ListPullRequestCommits",
  "ListReviewThreads",
  "ListReviewThreadComments",
];

function entry(state: string): PullRequestListEntry {
  return {
    association: { url: PR },
    row: { number: 42 },
    pr: { url: PR, state },
  } as unknown as PullRequestListEntry;
}

async function renderRows(served: readonly string[], state = "OPEN") {
  host.served = new Set(served);
  const entries = new Map([[PR, entry(state)]]);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  const view = renderHook(
    () => ({
      route: usePullRequestPageRoute(SESSION),
      rows: useRowStateChanges({
        conversationId: SESSION,
        entriesByUrl: entries,
        onOpen: () => {},
      }),
    }),
    { wrapper },
  );
  await waitFor(() => expect(view.result.current.route).not.toBe("loading"));
  view.rerender();
  return { view, entries };
}

/** The method and request of each host write sent so far. */
async function sent(): Promise<[string, unknown][]> {
  const requests: [string, unknown][] = [];
  const workspace = new Proxy(
    {},
    {
      get: (_target, method: string) => async (request: unknown) => {
        requests.push([method, request]);
        return { payload: {} };
      },
    },
  ) as unknown as WorkspaceClient;
  await Promise.all(host.write.mock.calls.map(([, rpc]) => rpc.call(workspace)));
  return requests;
}

beforeEach(() => {
  host.write.mockReset();
  host.toast.mockReset();
});
afterEach(() => {
  resetPullRequestPageRoutesForTests();
  resetLifecycleActionsForTests();
  resetStateChainsForTests();
});

describe("a row's Close and Reopen where the host serves the state change", () => {
  it("closes through the host once, and Undo reopens it through the host", async () => {
    host.write.mockResolvedValue({
      object: "session.github.pr_state",
      pr_url: PR,
      state: "CLOSED",
    });
    const { view, entries } = await renderRows([...READS, "SetPullRequestState"]);
    const close = view.result.current.rows.actionFor(entry("OPEN"), "#42 Fix the build");
    expect(close).toMatchObject({ id: "close", label: expect.any(String) });

    act(() => close!.run());
    await waitFor(() => expect(host.toast).toHaveBeenCalled());
    await expect(sent()).resolves.toEqual([
      ["setPullRequestState", { prUrl: PR, state: "closed" }],
    ]);
    const [message, options] = host.toast.mock.calls.at(-1)!;
    expect(message).toMatch(/^Closed /);
    expect(options.action.label).toBe("Undo");

    // The list reads the PR as closed by the time Undo is clicked.
    entries.set(PR, entry("CLOSED"));
    host.write.mockResolvedValue({ object: "session.github.pr_state", pr_url: PR, state: "OPEN" });
    act(() => options.action.onClick(undefined));
    await waitFor(() => expect(host.write).toHaveBeenCalledTimes(2));
    await expect(sent()).resolves.toEqual([
      ["setPullRequestState", { prUrl: PR, state: "closed" }],
      ["setPullRequestState", { prUrl: PR, state: "open" }],
    ]);
    await waitFor(() => expect(host.toast.mock.calls.at(-1)![0]).toMatch(/^Reopened /));
  });

  it("offers Reopen on a closed PR's row", async () => {
    const { view } = await renderRows([...READS, "SetPullRequestState"], "CLOSED");
    expect(view.result.current.rows.actionFor(entry("CLOSED"), "#42")).toMatchObject({
      id: "reopen",
    });
  });
});

describe("a row's Close and Reopen where the host doesn't serve the state change", () => {
  it("offers neither and sends nothing", async () => {
    const { view } = await renderRows(READS);
    expect(view.result.current.rows.actionFor(entry("OPEN"), "#42")).toBeNull();
    expect(view.result.current.rows.actionFor(entry("CLOSED"), "#42")).toBeNull();
    expect(host.write).not.toHaveBeenCalled();
  });
});
