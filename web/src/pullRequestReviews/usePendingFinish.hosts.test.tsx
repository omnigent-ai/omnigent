// Two sessions' hosts on the same pull request each finish their own pending review: an
// unconfirmed submit through one host never stands for, or pins, the other's.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

const host = vi.hoisted(() => ({ read: vi.fn(), discard: vi.fn() }));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => ({ features: {} }) }));
vi.mock("@/lib/workspaceApi", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  enabledWorkspaceRoutes: () => ["server"],
  servingRoutes: async (sessionId: string) => ({
    serving: [
      {
        route: "server",
        key: `server:${sessionId}`,
        served: new Set([
          "GetPullRequest",
          "ListPullRequestFiles",
          "GetPullRequestDiff",
          "ListPullRequestCommits",
          "ListReviewThreads",
          "ListReviewThreadComments",
          "GetPendingReview",
          "SubmitPendingReview",
          "DiscardPendingReview",
        ]),
      },
    ],
    failures: [],
  }),
}));
vi.mock("@/lib/hostPullRequestWrites", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  discardPendingReviewOnHost: host.discard,
}));
vi.mock("@/lib/pullRequestRpc", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  readPullRequestRpc: host.read,
}));

import { PullRequestWriteError } from "@/lib/pullRequestWriteAnswers";
import { draftOwner } from "@/lib/workspaceDraftOwner";
import {
  resetPullRequestPageRoutesForTests,
  usePullRequestPageRoute,
} from "@/pullRequestPage/pullRequestPageRoute";
import { HEAD, pendingAnswer } from "@/test/pendingReviewAnswers";

import { hostPendingIdentity, hostPendingRoute, hostPendingScopeOf } from "./hostPendingRoute";
import { resetPendingReviewChangesForTests, sendPendingReviewChange } from "./pendingReviewChanges";
import { pendingSubmitKey, resetReviewSubmitForTests, updateReviewSubmit } from "./reviewSubmit";
import { usePendingFinish } from "./usePendingFinish";

const PR = "https://github.com/acme/app/pull/42";

function renderFinish(sessionId: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return renderHook(
    () => {
      const route = usePullRequestPageRoute(sessionId);
      const hostRoute = route && route !== "loading" ? hostPendingRoute(sessionId, PR) : null;
      return usePendingFinish({
        conversationId: sessionId,
        prUrl: PR,
        hostRoute,
        open: false,
      });
    },
    { wrapper },
  );
}

afterEach(() => {
  resetPullRequestPageRoutesForTests();
  resetReviewSubmitForTests();
  resetPendingReviewChangesForTests();
});

describe("pending reviews through two sessions' hosts", () => {
  it("leaves one host's unconfirmed submit out of the other's menu", async () => {
    host.read.mockImplementation(async (sessionId: string) =>
      pendingAnswer({ reviewId: sessionId === "session-a" ? "PRR_a" : "PRR_b" }),
    );
    // Host A's menu read its review, and its submit went unconfirmed.
    const a = renderFinish("session-a");
    await waitFor(() => expect(a.result.current?.reviewId).toBe("PRR_a"));
    const scopeA = hostPendingScopeOf(hostPendingRoute("session-a", PR)!);
    updateReviewSubmit(
      pendingSubmitKey(draftOwner(), PR, { login: hostPendingIdentity(scopeA) }, "PRR_a"),
      (entry) => ({
        ...entry,
        status: {
          kind: "uncertain",
          sent: { event: "COMMENT", body: "", head: HEAD, knownReviewIds: null, sentAt: 0 },
          message: "GitHub didn’t confirm it.",
          checking: false,
          checked: "never",
        },
      }),
    );
    await waitFor(() => expect(a.result.current?.unresolved).not.toBeNull());

    // Host B finishes its own review, with nothing of A's pinned.
    const b = renderFinish("session-b");
    await waitFor(() => expect(b.result.current?.reviewId).toBe("PRR_b"));
    expect(b.result.current?.unresolved).toBeNull();
  });

  it("leaves one host's held discard out of the other's review", async () => {
    host.read.mockImplementation(async (sessionId: string) =>
      pendingAnswer({ reviewId: sessionId === "session-a" ? "PRR_a" : "PRR_b" }),
    );
    host.discard.mockRejectedValue(new PullRequestWriteError("outcome_unknown", "No answer."));
    // Host A's discard went unanswered, so it's held for a check.
    const a = renderFinish("session-a");
    await waitFor(() => expect(a.result.current?.reviewId).toBe("PRR_a"));
    const outcome = await sendPendingReviewChange(PR, hostPendingRoute("session-a", PR)!, {
      kind: "discard",
      reviewId: "PRR_a",
    });
    expect(outcome.kind).toBe("held");
    await waitFor(() => expect(a.result.current?.unresolved?.kind).toBe("discard"));

    // Host B's menu shows its own review, with nothing of A's discard on it.
    const b = renderFinish("session-b");
    await waitFor(() => expect(b.result.current?.reviewId).toBe("PRR_b"));
    expect(b.result.current?.unresolved).toBeNull();
  });
});
