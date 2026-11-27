// Close, reopen, draft, ready and turning auto-merge off without this Mac's shell: where the
// session's host lists the method for this caller, the page offers the change and sends it
// through the host once; where it doesn't, as for an editor, it offers GitHub instead.

import type * as SonnerModule from "sonner";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const host = vi.hoisted(() => ({
  served: new Set<string>(),
  write: vi.fn(),
  read: vi.fn(),
}));
vi.mock("sonner", async (importOriginal) => ({
  ...(await importOriginal<typeof SonnerModule>()),
  toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }),
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
  readPullRequestRpc: host.read,
}));

import type { WorkspaceClient } from "@/lib/workspaceApi";
import { PullRequestMaybeSentError } from "@/lib/workspaceApi";

import {
  LifecycleSurfaceContext,
  resetLifecycleActionsForTests,
} from "./pullRequestLifecycleActions";
import {
  resetPullRequestPageRoutesForTests,
  usePullRequestPageRoute,
} from "./pullRequestPageRoute";
import { resetStateChainsForTests } from "./pullRequestStateChain";
import {
  type LifecyclePullRequest,
  usePullRequestLifecycleActions,
} from "./usePullRequestLifecycleActions";
import { usePullRequestStateAction } from "./usePullRequestStateAction";

const PR = "https://github.com/acme/app/pull/42";
const SESSION = "session-1";
const HEAD = "a".repeat(40);

const READS = [
  "GetPullRequest",
  "ListPullRequestFiles",
  "GetPullRequestDiff",
  "ListPullRequestCommits",
  "ListReviewThreads",
  "ListReviewThreadComments",
];
// What next's levels give each caller: EDIT-level writes for an editor, every one for the owner.
const EDITOR = [...READS, "GetMergeOptions", "DisableAutoMerge", "SetReviewThreadResolved"];
const OWNER = [...EDITOR, "SetPullRequestState", "SetPullRequestDraft", "EnableAutoMerge"];

function pullRequest(over: Partial<LifecyclePullRequest> = {}): LifecyclePullRequest {
  return {
    conversationId: SESSION,
    prUrl: PR,
    number: 42,
    state: "OPEN",
    isDraft: false,
    headSha: HEAD,
    autoMergeOn: false,
    mergeable: true,
    ...over,
  };
}

/** The page's route decided as `served`, then the lifecycle and Close/Reopen controllers. */
async function renderPage(served: readonly string[], facts = pullRequest()) {
  host.served = new Set(served);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <LifecycleSurfaceContext.Provider value="surface-1">
        {children}
      </LifecycleSurfaceContext.Provider>
    </QueryClientProvider>
  );
  const view = renderHook(
    () => ({
      route: usePullRequestPageRoute(SESSION),
      actions: usePullRequestLifecycleActions(facts),
      state: usePullRequestStateAction({
        conversationId: SESSION,
        prUrl: PR,
        prState: facts.state,
        post: async () => true,
      }),
    }),
    { wrapper },
  );
  await waitFor(() => expect(view.result.current.route).not.toBe("loading"));
  view.rerender();
  return view;
}

/** The requests each host write sent, by method. */
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
  host.read.mockReset();
  host.read.mockResolvedValue({
    available: true,
    selected_pr_url: PR,
    pr: { url: PR, state: "OPEN" },
  });
});
afterEach(() => {
  resetPullRequestPageRoutesForTests();
  resetLifecycleActionsForTests();
  resetStateChainsForTests();
});

describe("state changes through the session's host, as the owner", () => {
  it("offers Convert to draft and sends it once, after asking", async () => {
    host.write.mockResolvedValue({ object: "session.github.pr_draft", pr_url: PR, is_draft: true });
    const { result } = await renderPage(OWNER);
    expect(result.current.actions.availability.convert_to_draft).toEqual({ kind: "mac" });
    act(() => result.current.actions.request("convert_to_draft"));
    expect(host.write).not.toHaveBeenCalled();
    act(() => result.current.actions.confirm());
    await waitFor(() => expect(result.current.actions.state.running).toBeNull());
    expect(host.write).toHaveBeenCalledTimes(1);
    await expect(sent()).resolves.toEqual([["setPullRequestDraft", { prUrl: PR, draft: true }]]);
    expect(result.current.actions.state.notice).toBeNull();
  });

  it("marks a draft ready at once", async () => {
    host.write.mockResolvedValue({
      object: "session.github.pr_draft",
      pr_url: PR,
      is_draft: false,
    });
    const { result } = await renderPage(OWNER, pullRequest({ isDraft: true }));
    act(() => result.current.actions.request("ready_for_review"));
    await waitFor(() => expect(host.write).toHaveBeenCalledTimes(1));
    await expect(sent()).resolves.toEqual([["setPullRequestDraft", { prUrl: PR, draft: false }]]);
  });

  it("turns auto-merge off through the host", async () => {
    host.write.mockResolvedValue({ object: "session.github.pr_merge", pr_url: PR });
    const { result } = await renderPage(OWNER, pullRequest({ autoMergeOn: true }));
    expect(result.current.actions.availability.disable_auto_merge).toEqual({ kind: "mac" });
    act(() => result.current.actions.request("disable_auto_merge"));
    await waitFor(() => expect(host.write).toHaveBeenCalledTimes(1));
    await expect(sent()).resolves.toEqual([["disableAutoMerge", { prUrl: PR }]]);
  });

  it("offers Close and sends it once through the host", async () => {
    host.write.mockResolvedValue({
      object: "session.github.pr_state",
      pr_url: PR,
      state: "CLOSED",
    });
    const { result } = await renderPage(OWNER);
    expect(result.current.state.offer).toMatchObject({
      kind: "action",
      operation: "pull_request_close",
    });
    act(() => result.current.state.run(false));
    await waitFor(() => expect(result.current.state.running).toBe(false));
    expect(host.write).toHaveBeenCalledTimes(1);
    await expect(sent()).resolves.toEqual([
      ["setPullRequestState", { prUrl: PR, state: "closed" }],
    ]);
    expect(result.current.state.notice).toBeNull();
  });

  it("offers Reopen for a closed PR", async () => {
    const { result } = await renderPage(OWNER, pullRequest({ state: "CLOSED" }));
    expect(result.current.state.offer).toMatchObject({
      kind: "action",
      operation: "pull_request_reopen",
    });
  });

  it("holds a change that may have reached the host until Check Again reads it through the host", async () => {
    host.write.mockRejectedValue(new PullRequestMaybeSentError(new TypeError("dropped")));
    const { result } = await renderPage(OWNER, pullRequest({ isDraft: true }));
    act(() => result.current.actions.request("ready_for_review"));
    await waitFor(() => expect(result.current.actions.state.notice?.outcome.kind).toBe("unknown"));
    // Nothing more goes while GitHub may have made it, Close included.
    act(() => result.current.state.run(false));
    expect(host.write).toHaveBeenCalledTimes(1);
    host.read.mockResolvedValue({
      available: true,
      selected_pr_url: PR,
      pr: { url: PR, state: "OPEN", is_draft: false },
    });
    await act(() => result.current.actions.checkAgain());
    expect(result.current.actions.state.notice).toBeNull();
    expect(host.write).toHaveBeenCalledTimes(1);
  });

  it("keeps a refusal's reason, and doesn't send it again on its own", async () => {
    const { PullRequestApiError } = await import("@/lib/pullRequestRpc");
    host.write.mockRejectedValue(new PullRequestApiError("Only the author can do this.", 403));
    const { result } = await renderPage(OWNER, pullRequest({ isDraft: true }));
    act(() => result.current.actions.request("ready_for_review"));
    await waitFor(() =>
      expect(result.current.actions.state.notice?.outcome).toMatchObject({
        kind: "refused",
        message: "Only the author can do this.",
      }),
    );
    expect(host.write).toHaveBeenCalledTimes(1);
  });

  it("sends nothing once the page's route is no longer the one the dialog opened on", async () => {
    const { result } = await renderPage(OWNER);
    act(() => result.current.actions.request("convert_to_draft"));
    // The route the click captured is gone, as after the flags turned it off.
    resetPullRequestPageRoutesForTests();
    act(() => result.current.actions.confirm());
    expect(host.write).not.toHaveBeenCalled();
    expect(result.current.actions.state.notice?.outcome.kind).toBe("skipped");
  });
});

describe("state changes as an editor, whose host lists only EDIT-level writes", () => {
  it("offers GitHub for Convert, Ready and Close, and turning auto-merge off here", async () => {
    const open = await renderPage(EDITOR, pullRequest({ autoMergeOn: true }));
    expect(open.result.current.actions.availability.convert_to_draft).toEqual({
      kind: "github",
      url: PR,
    });
    expect(open.result.current.actions.availability.disable_auto_merge).toEqual({ kind: "mac" });
    expect(open.result.current.state.offer).toMatchObject({ kind: "link", url: PR });
    open.unmount();
    const draft = await renderPage(EDITOR, pullRequest({ isDraft: true }));
    expect(draft.result.current.actions.availability.ready_for_review).toEqual({
      kind: "github",
      url: PR,
    });
  });

  it("sends nothing from a request the host wouldn't make for them", async () => {
    const { result } = await renderPage(EDITOR, pullRequest({ isDraft: true }));
    act(() => result.current.actions.request("ready_for_review"));
    act(() => result.current.state.run(false));
    expect(host.write).not.toHaveBeenCalled();
  });
});

describe("without the PR methods", () => {
  it("offers GitHub where the session shows the classic page's route", async () => {
    const { result } = await renderPage([]);
    expect(result.current.route).toBeNull();
    expect(result.current.actions.availability.convert_to_draft).toEqual({
      kind: "github",
      url: PR,
    });
    expect(result.current.state.offer).toMatchObject({ kind: "link" });
  });
});
