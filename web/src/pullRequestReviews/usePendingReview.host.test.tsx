// The pending review read through the session's host, where this shell has no route: one
// GetPendingReview for the PR, parsed as the Mac's answer, and read again only on focus, after a
// change or on Refresh, never on a timer. A caller whose list lacks the read sees none.

import { focusManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const host = vi.hoisted(() => ({ served: new Set<string>(), read: vi.fn(), write: vi.fn() }));
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
  writePullRequestRpc: host.write,
}));

import { PullRequestApiError } from "@/lib/pullRequestRpc";
import type { WorkspaceClient } from "@/lib/workspaceApi";
import {
  resetPullRequestPageRoutesForTests,
  usePullRequestPageRoute,
} from "@/pullRequestPage/pullRequestPageRoute";
import {
  BASE,
  draftThread,
  HEAD,
  noReviewAnswer,
  pendingAnswer,
  REVIEW_ID,
} from "@/test/pendingReviewAnswers";

import { resetPendingRecoveriesForTests } from "./pendingComposer";
import type { DraftPlace } from "./pendingReview";
import { resetPendingReviewChangesForTests } from "./pendingReviewChanges";
import { usePendingComposer } from "./usePendingComposer";
import { invalidatePendingReview, usePendingReview } from "./usePendingReview";

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
      pending: usePendingReview(PR, true, conversationId),
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
  host.write.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
  resetPullRequestPageRoutesForTests();
  resetPendingRecoveriesForTests();
  resetPendingReviewChangesForTests();
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
    const { state, hostRoute } = view.result.current.pending;
    expect(state.kind === "pending" && state.review.id).toBe(REVIEW_ID);
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
      await vi.advanceTimersByTimeAsync(15_000 * 8);
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

describe("a comment box whose start made a review but didn't add its comment", () => {
  const LINES: DraftPlace = {
    kind: "lines",
    path: "src/app.ts",
    side: "RIGHT",
    line: 12,
    startLine: null,
    headSha: HEAD,
    baseSha: BASE,
  };
  const DRAFT_KEY = ["pr-draft", SESSION, PR, "line"];

  async function renderComposer(served: readonly string[] = EDITOR) {
    host.served = new Set(served);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    queryClient.setQueryData(DRAFT_KEY, { body: "Why strip?", pending: false, error: null });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
    const view = renderHook(
      () => ({
        route: usePullRequestPageRoute(SESSION),
        composer: usePendingComposer({
          conversationId: SESSION,
          prUrl: PR,
          place: LINES,
          draftKey: DRAFT_KEY,
          head: HEAD,
          post: () => {},
          commentLabel: "Comment",
        }),
      }),
      { wrapper },
    );
    await waitFor(() => expect(view.result.current.route).not.toBe("loading"));
    view.rerender();
    return { view, queryClient };
  }

  it.each([
    ["is submitted or discarded", noReviewAnswer()],
    ["is replaced by another", pendingAnswer({ reviewId: "PRR_other" })],
  ])("lets the text go once a later read shows the review %s", async (_case, later) => {
    host.read.mockResolvedValue(noReviewAnswer());
    host.write
      .mockResolvedValueOnce({
        object: "session.github.pr_review_start",
        pr_url: PR,
        review_id: REVIEW_ID,
        commit_oid: HEAD,
      })
      .mockRejectedValueOnce(new PullRequestApiError("That line isn't in the diff.", 400));
    const { view, queryClient } = await renderComposer();
    await waitFor(() => expect(view.result.current.composer?.mode).toBe("comment_or_start"));
    host.read.mockResolvedValue(pendingAnswer());
    act(() => view.result.current.composer!.secondary!.run());
    await waitFor(() => expect(view.result.current.composer?.mode).toBe("recovering"));

    // Read after the start, so it speaks for the review the start made.
    await new Promise((resolve) => {
      setTimeout(resolve, 5);
    });
    host.read.mockResolvedValue(later);
    act(() => invalidatePendingReview(queryClient, PR));
    await waitFor(() => expect(view.result.current.composer?.mode).toBe("recovery_gone"));
    const reuse = view.result.current.composer!.notice!.actions[0]!;
    expect(reuse.label).toBe("Use for a new comment");
    act(() => reuse.run());
    expect(view.result.current.composer?.mode).not.toBe("recovery_gone");
  });

  const READ_ONLY = [...READS, "GetPendingReview"];
  it.each([
    ["no start or add", READ_ONLY, noReviewAnswer(), null],
    ["a start but no add", [...READ_ONLY, "StartPendingReview"], noReviewAnswer(), null],
    ["an add but no start", [...READ_ONLY, "AddPendingReviewComment"], noReviewAnswer(), null],
    ["both", EDITOR, noReviewAnswer(), "comment_or_start"],
    ["no add, with a review pending", READ_ONLY, pendingAnswer(), null],
    [
      "an add, with a review pending",
      [...READ_ONLY, "AddPendingReviewComment"],
      pendingAnswer(),
      "add_to_review",
    ],
  ])("offers pending review actions only where served: %s", async (_case, served, read, mode) => {
    host.read.mockResolvedValue(read);
    const { view } = await renderComposer(served);
    await waitFor(() => expect(host.read).toHaveBeenCalled());
    await waitFor(() => expect(view.result.current.composer?.mode ?? null).toBe(mode));
    // Where nothing's offered, the box comments as it does today.
    expect(view.result.current.composer === null).toBe(mode === null);
  });
});
