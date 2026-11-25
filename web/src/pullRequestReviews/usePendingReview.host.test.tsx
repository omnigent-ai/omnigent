// The pending review read through the session's host, where this shell has no route: one
// GetPendingReview for the PR, parsed as the Mac's answer, and read again only on focus, after a
// change or on Refresh, never on a timer. A caller whose list lacks the read sees none.

import { focusManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const host = vi.hoisted(() => ({ served: new Set<string>(), read: vi.fn() }));
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
  readPullRequestRpc: host.read,
}));

import type { WorkspaceClient } from "@/lib/workspaceApi";
import {
  resetPullRequestPageRoutesForTests,
  usePullRequestPageRoute,
} from "@/pullRequestPage/pullRequestPageRoute";
import { draftThread, noReviewAnswer, pendingAnswer, REVIEW_ID } from "@/test/pendingReviewAnswers";

import {
  invalidatePendingReview,
  PENDING_REVIEW_POLL_MS,
  usePendingReview,
} from "./usePendingReview";

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
// The read is an editor's and the owner's; a reader's list leaves it out.
const VIEWER = READS;
const EDITOR = [...READS, "GetPendingReview", "StartPendingReview", "AddPendingReviewComment"];
const OWNER = [...EDITOR, "DeletePendingReviewComment", "SubmitPendingReview"];

async function renderPending(served: readonly string[], conversationId = SESSION) {
  host.served = new Set(served);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  const view = renderHook(
    () => ({
      route: usePullRequestPageRoute(SESSION),
      pending: usePendingReview(PR, null, true, conversationId),
    }),
    { wrapper },
  );
  await waitFor(() => expect(view.result.current.route).not.toBe("loading"));
  view.rerender();
  return { view, queryClient };
}

/** The request the read sent through the workspace client. */
async function sentRequest(call = 0): Promise<{ method: string; request: unknown }> {
  const [, method, read] = host.read.mock.calls[call]!;
  let request: unknown;
  await read({
    getPendingReview: (input: unknown) => {
      request = input;
      return Promise.resolve({});
    },
  } as unknown as WorkspaceClient);
  return { method, request };
}

beforeEach(() => {
  host.read.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
  resetPullRequestPageRoutesForTests();
});

describe("usePendingReview through the session's host", () => {
  it.each([
    ["an editor", EDITOR],
    ["the owner", OWNER],
  ])("reads %s's pending review once, in the Mac's shape", async (_who, served) => {
    host.read.mockResolvedValue(
      pendingAnswer({ threads: [draftThread("PRRT_1", [{ id: "PRRC_1", body: "Why?" }])] }),
    );
    const { view } = await renderPending(served);

    await waitFor(() => expect(view.result.current.pending.state.kind).toBe("pending"));
    const { state, hostRoute, route } = view.result.current.pending;
    expect(state.kind === "pending" && state.review.id).toBe(REVIEW_ID);
    expect(route).toBeNull();
    expect(hostRoute).toMatchObject({ kind: "host", conversationId: SESSION, prUrl: PR });
    expect(host.read).toHaveBeenCalledTimes(1);
    expect(host.read.mock.calls[0]![0]).toBe(SESSION);
    expect(await sentRequest()).toEqual({ method: "getPendingReview", request: { prUrl: PR } });
  });

  it("offers nothing and reads nothing for a caller whose list lacks the read", async () => {
    const { view } = await renderPending(VIEWER);

    expect(view.result.current.pending.state.kind).toBe("unavailable");
    expect(view.result.current.pending.hostRoute).toBeNull();
    expect(host.read).not.toHaveBeenCalled();
  });

  it("reads nothing for a view outside any session", async () => {
    const { view } = await renderPending(EDITOR, "");

    expect(view.result.current.pending.state.kind).toBe("unavailable");
    expect(host.read).not.toHaveBeenCalled();
  });

  it("is never read on a timer, only on focus and after a change", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    host.read.mockResolvedValue(noReviewAnswer());
    const { view, queryClient } = await renderPending(EDITOR);
    await waitFor(() => expect(view.result.current.pending.state.kind).toBe("none"));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(PENDING_REVIEW_POLL_MS * 8);
    });
    expect(host.read).toHaveBeenCalledTimes(1);

    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });
    await waitFor(() => expect(host.read).toHaveBeenCalledTimes(2));

    act(() => invalidatePendingReview(queryClient, PR));
    await waitFor(() => expect(host.read).toHaveBeenCalledTimes(3));
    focusManager.setFocused(undefined);
  });

  it("shows a read it can't parse as an error, never as no review", async () => {
    host.read.mockResolvedValue({ object: "session.github.pending_review", comments: [] });
    const { view } = await renderPending(EDITOR);

    await waitFor(() => expect(view.result.current.pending.state.kind).toBe("error"));
  });
});
