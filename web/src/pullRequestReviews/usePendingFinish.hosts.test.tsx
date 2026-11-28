// Two sessions' hosts on the same pull request each finish their own pending review: an
// unconfirmed submit through one host never stands for, or pins, the other's. Their comment
// boxes keep apart the same way, though neither host names a login.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

const host = vi.hoisted(() => ({ read: vi.fn(), discard: vi.fn(), add: vi.fn() }));
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
          "AddPendingReviewComment",
        ]),
      },
    ],
    failures: [],
  }),
}));
vi.mock("@/lib/hostPullRequestWrites", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  discardPendingReviewOnHost: host.discard,
  addDraftOnHost: host.add,
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
import { BASE, HEAD, noReviewAnswer, pendingAnswer } from "@/test/pendingReviewAnswers";

import { hostPendingIdentity, hostPendingRoute, hostPendingScopeOf } from "./hostPendingRoute";
import {
  pendingRecoveriesOf,
  recordPendingRecovery,
  resetPendingRecoveriesForTests,
  resolvePendingRecoveries,
} from "./pendingComposer";
import type { DraftPlace } from "./pendingReview";
import {
  type PendingSentAs,
  resetPendingReviewChangesForTests,
  sendPendingReviewChange,
} from "./pendingReviewChanges";
import { pendingSubmitKey, resetReviewSubmitForTests, updateReviewSubmit } from "./reviewSubmit";
import { usePendingComposer } from "./usePendingComposer";
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
  resetPendingRecoveriesForTests();
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

describe("comment boxes through two sessions' hosts", () => {
  const LINES: DraftPlace = {
    kind: "lines",
    path: "src/app.ts",
    side: "RIGHT",
    line: 12,
    startLine: null,
    headSha: HEAD,
    baseSha: BASE,
  };

  function renderComposer(sessionId: string) {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
    return renderHook(
      () => {
        usePullRequestPageRoute(sessionId);
        return usePendingComposer({
          conversationId: sessionId,
          prUrl: PR,
          place: LINES,
          draftKey: ["pr-draft", sessionId, PR, "line"],
          head: HEAD,
          post: () => {},
          commentLabel: "Comment",
        });
      },
      { wrapper },
    );
  }

  /** Both boxes on their own review: A's is PRR_a, B's PRR_b. */
  async function renderBoth() {
    host.read.mockImplementation(async (sessionId: string) =>
      pendingAnswer({ reviewId: sessionId === "session-a" ? "PRR_a" : "PRR_b" }),
    );
    const a = renderComposer("session-a");
    const b = renderComposer("session-b");
    await waitFor(() => expect(a.result.current?.mode).toBe("add_to_review"));
    await waitFor(() => expect(b.result.current?.mode).toBe("add_to_review"));
    return { a, b };
  }

  // Each host's sender, with the empty login a host route reads as.
  const sentAs = (sessionId: string): PendingSentAs => ({
    login: "",
    host: hostPendingScopeOf(hostPendingRoute(sessionId, PR)!),
  });

  it("keeps one host's half-done start to its own box, and out of the other's reads", async () => {
    const { a, b } = await renderBoth();
    const owner = draftOwner();
    const started = (sessionId: string) =>
      recordPendingRecovery({
        owner,
        prUrl: PR,
        sentAs: sentAs(sessionId),
        reviewId: `PRR_${sessionId}`,
        place: LINES,
        sent: null,
      });
    started("session-a");
    await waitFor(() => expect(a.result.current?.mode).toBe("recovering"));
    expect(b.result.current?.mode).toBe("add_to_review");

    // B's own start at the same place leaves A's kept, and B's read says nothing of A's.
    started("session-b");
    expect(pendingRecoveriesOf(owner, PR)).toHaveLength(2);
    resolvePendingRecoveries(owner, PR, sentAs("session-b"), {
      ...noReviewAnswer(),
      issued_at: Date.now() + 1_000,
    });
    expect(
      pendingRecoveriesOf(owner, PR).map((recovery) => [recovery.reviewId, recovery.gone]),
    ).toEqual([
      ["PRR_session-a", false],
      ["PRR_session-b", true],
    ]);
  });

  it("keeps one host's unconfirmed add out of the other's box", async () => {
    const { a, b } = await renderBoth();
    host.add.mockRejectedValue(new PullRequestWriteError("outcome_unknown", "No answer."));
    const outcome = await sendPendingReviewChange(PR, hostPendingRoute("session-a", PR)!, {
      kind: "draft_add",
      body: "Why strip?",
      place: LINES,
      pendingReviewId: "PRR_a",
      expectedHead: HEAD,
    });
    expect(outcome.kind).toBe("held");
    await waitFor(() => expect(a.result.current?.mode).toBe("held"));
    expect(b.result.current?.mode).toBe("add_to_review");
  });
});
