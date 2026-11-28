// A host serves each caller its own methods, so a session's route is decided for the signed-in
// user only. Signing out or switching users drops it at once, with no reload: no read or change
// goes through the last user's methods, and nothing the last user started is the next one's.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const host = vi.hoisted(() => ({
  user: "owner-1" as string | null,
  listeners: new Set<() => void>(),
  served: new Map<string | null, Set<string>>(),
  servingRoutes: vi.fn(),
  write: vi.fn(),
  read: vi.fn(),
  toast: vi.fn(),
}));
vi.mock("@/components/ui/toast", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  showToast: host.toast,
}));
vi.mock("@/lib/pullRequestRpc", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  writePullRequestRpc: host.write,
  readPullRequestRpc: host.read,
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getCurrentUserId: () => host.user,
  subscribeIdentity: (listener: () => void) => {
    host.listeners.add(listener);
    return () => host.listeners.delete(listener);
  },
}));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => ({ features: {} }) }));
vi.mock("@/lib/workspaceApi", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  enabledWorkspaceRoutes: () => ["server"],
  servingRoutes: host.servingRoutes,
}));

import { PullRequestWriteError } from "@/lib/pullRequestWriteAnswers";
import { discardPendingReviewOnHost, HOST_ROUTE_GONE } from "@/lib/hostPullRequestWrites";
import { PullRequestMaybeSentError } from "@/lib/workspaceApi";
import { pendingScopeCurrent } from "@/pullRequestComments/commentEditScope";
import {
  HOST_PENDING_METHODS,
  hostPendingOffers,
  hostPendingRoute,
  hostPendingScopeOf,
} from "@/pullRequestReviews/hostPendingRoute";
import { hostReviewRoute } from "@/pullRequestReviews/hostReviews";
import {
  checkPendingHold,
  pendingHoldsOf,
  resetPendingReviewChangesForTests,
  sendPendingReviewChange,
} from "@/pullRequestReviews/pendingReviewChanges";
import { readReviewSubmit, resetReviewSubmitForTests } from "@/pullRequestReviews/reviewSubmit";
import { sendReviewOnHost } from "@/pullRequestReviews/sendReview";
import { usePendingReview } from "@/pullRequestReviews/usePendingReview";
import { BASE, HEAD, pendingAnswer, REVIEW_ID } from "@/test/pendingReviewAnswers";

import {
  hostChangeRoute,
  resetPullRequestPageRoutesForTests,
  usePullRequestPageRoute,
} from "./pullRequestPageRoute";

const SESSION = "session-1";
const PR = "https://github.com/acme/app/pull/42";
const READS = [
  "GetPullRequest",
  "ListPullRequestFiles",
  "GetPullRequestDiff",
  "ListPullRequestCommits",
  "ListReviewThreads",
  "ListReviewThreadComments",
];
// Only a shared session's owner writes to GitHub; anyone else the host serves reads only.
const MEMBER = [...READS, "GetPendingReview"];
const OWNER = [
  ...MEMBER,
  "StartPendingReview",
  "AddPendingReviewComment",
  "UpdatePendingReviewComment",
  "DeletePendingReviewComment",
  "SubmitPendingReview",
  "DiscardPendingReview",
  "SubmitReview",
];
const WRITE_STEPS = ["start", "add", "edit", "delete", "submit", "discard"] as const;
const WRITE_METHODS = [
  "startPendingReview",
  "addPendingReviewComment",
  "updatePendingReviewComment",
  "deletePendingReviewComment",
  "submitPendingReview",
  "discardPendingReview",
  "submitReview",
] as const;
const lost = () => new PullRequestMaybeSentError("The connection closed.");

function signInAs(user: string | null) {
  act(() => {
    host.user = user;
    for (const listener of host.listeners) listener();
  });
}

function renderRoute() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(() => usePullRequestPageRoute(SESSION), { wrapper });
}

/** The page's route and its pending review, as the PR page renders them. */
function renderPending() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const view = renderHook(
    () => ({
      route: usePullRequestPageRoute(SESSION),
      pending: usePendingReview(PR, true, SESSION),
    }),
    { wrapper },
  );
  return { view, client };
}

/** The owner's route for the PR, once decided. */
async function ownerRoute() {
  renderRoute();
  await waitFor(() => expect(hostPendingRoute(SESSION, PR)).not.toBeNull());
  return hostPendingRoute(SESSION, PR)!;
}

beforeEach(() => {
  host.write.mockReset();
  host.read.mockReset();
  host.servingRoutes.mockClear();
  host.toast.mockReset();
  host.user = "owner-1";
  host.served = new Map([
    ["owner-1", new Set(OWNER)],
    ["member-1", new Set(MEMBER)],
  ]);
  // Each answer is the methods the host serves the user signed in when it's asked.
  host.servingRoutes.mockImplementation(async () => {
    const served = host.served.get(host.user);
    return served
      ? { serving: [{ route: "server", key: `server:${SESSION}`, served }], failures: [] }
      : { serving: [], failures: [new Error("Sign in again.")] };
  });
});

afterEach(() => {
  resetPullRequestPageRoutesForTests();
  resetPendingReviewChangesForTests();
  resetReviewSubmitForTests();
  host.listeners.clear();
});

describe("a session's route across a change of user", () => {
  it("drops the owner's methods the moment another user signs in, then offers them no write", async () => {
    let gate!: () => void;
    const view = renderRoute();
    await waitFor(() => expect(hostChangeRoute(SESSION, "submitPendingReview")).not.toBeNull());
    const ownerScope = hostPendingScopeOf(hostPendingRoute(SESSION, PR)!);

    host.servingRoutes.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          gate = () =>
            resolve({
              serving: [{ route: "server", key: `server:${SESSION}`, served: new Set(MEMBER) }],
              failures: [],
            });
        }),
    );
    signInAs("member-1");
    // Before their answer, nothing goes through the owner's decision.
    for (const method of WRITE_METHODS) expect(hostChangeRoute(SESSION, method), method).toBeNull();
    expect(view.result.current).toBe("loading");

    await act(async () => gate());
    await waitFor(() => expect(view.result.current).toMatchObject({ route: "server" }));
    expect(hostChangeRoute(SESSION, "getPendingReview")).not.toBeNull();
    for (const method of WRITE_METHODS) expect(hostChangeRoute(SESSION, method), method).toBeNull();
    // Whatever the owner captured, such as a held draft, isn't theirs to show or act on.
    expect(pendingScopeCurrent({ host: ownerScope }, PR)).toBe(false);
  });

  it("drops the route on sign-out and sends nothing through it", async () => {
    const view = renderRoute();
    await waitFor(() => expect(hostChangeRoute(SESSION, "addPendingReviewComment")).not.toBeNull());
    const ownerScope = hostPendingScopeOf(hostPendingRoute(SESSION, PR)!);

    signInAs(null);

    expect(hostChangeRoute(SESSION, "addPendingReviewComment")).toBeNull();
    expect(hostPendingRoute(SESSION, PR)).toBeNull();
    await waitFor(() => expect(view.result.current).toBeNull());
    expect(pendingScopeCurrent({ host: ownerScope }, PR)).toBe(false);
  });

  it("is the owner's again, under a fresh key, when the owner signs back in", async () => {
    const view = renderRoute();
    await waitFor(() => expect(view.result.current).toMatchObject({ route: "server" }));
    const first = hostChangeRoute(SESSION, "submitPendingReview")!;

    signInAs("member-1");
    await waitFor(() => expect(hostChangeRoute(SESSION, "getPendingReview")).not.toBeNull());
    signInAs("owner-1");

    await waitFor(() => expect(hostChangeRoute(SESSION, "submitPendingReview")).not.toBeNull());
    expect(hostChangeRoute(SESSION, "submitPendingReview")!.key).toBe(first.key);
    expect(hostChangeRoute(SESSION, "addPendingReviewComment")!.key).not.toContain("member-1");
    expect(hostChangeRoute(SESSION, "addPendingReviewComment")!.key).toBe(first.key);
  });
});

describe("the pending review across a change of user, with no reload", () => {
  it("reads and sends nothing after a sign-out, for any step", async () => {
    const route = await ownerRoute();
    host.read.mockResolvedValue(pendingAnswer());

    signInAs(null);

    for (const step of Object.keys(HOST_PENDING_METHODS) as (keyof typeof HOST_PENDING_METHODS)[]) {
      expect(hostPendingOffers(route, step), step).toBe(false);
    }
    const { view } = renderPending();
    expect(view.result.current.pending.hostRoute).toBeNull();
    expect(
      await sendPendingReviewChange(PR, route, { kind: "discard", reviewId: REVIEW_ID }),
    ).toMatchObject({ kind: "refused" });
    // Even a caller holding the old route can't send through it.
    await expect(
      discardPendingReviewOnHost(SESSION, { prUrl: PR, pendingReviewId: REVIEW_ID }),
    ).rejects.toEqual(new PullRequestWriteError("route_changed", HOST_ROUTE_GONE));
    expect(host.write).not.toHaveBeenCalled();
    expect(host.read).not.toHaveBeenCalled();
  });

  it("reads the next user's methods and review again, offering them no write", async () => {
    host.read.mockImplementation(async () =>
      host.user === "owner-1" ? pendingAnswer() : pendingAnswer({ reviewId: "PRR_members" }),
    );
    const { view } = renderPending();
    await waitFor(() => expect(view.result.current.pending.state.kind).toBe("pending"));
    expect(host.servingRoutes).toHaveBeenCalledTimes(1);

    signInAs("member-1");
    // The owner's review isn't shown while the next user's is read.
    const shown = view.result.current.pending.state;
    expect(shown.kind === "pending" && shown.review.id === REVIEW_ID).toBe(false);

    await waitFor(() =>
      expect(
        view.result.current.pending.state.kind === "pending" &&
          view.result.current.pending.state.review.id,
      ).toBe("PRR_members"),
    );
    expect(host.servingRoutes).toHaveBeenCalledTimes(2);
    const member = view.result.current.pending.hostRoute!;
    for (const step of WRITE_STEPS) expect(hostPendingOffers(member, step), step).toBe(false);
  });

  it("drops what lands after the owner signed out: no hold and no toast for the next user", async () => {
    const route = await ownerRoute();
    let answer!: (value: unknown) => void;
    let fail!: (error: unknown) => void;
    host.write
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            answer = resolve;
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise((_resolve, reject) => {
            fail = reject;
          }),
      );

    const discard = sendPendingReviewChange(PR, route, { kind: "discard", reviewId: REVIEW_ID });
    await waitFor(() => expect(host.write).toHaveBeenCalledTimes(1));
    signInAs("member-1");
    answer({ object: "session.github.pr_review_discard", review_id: REVIEW_ID });
    expect(await discard).toEqual({ kind: "suppressed" });

    signInAs("owner-1");
    await waitFor(() => expect(hostPendingRoute(SESSION, PR)).not.toBeNull());
    const add = sendPendingReviewChange(PR, hostPendingRoute(SESSION, PR)!, {
      kind: "draft_add",
      body: "Why?",
      place: {
        kind: "lines",
        path: "src/parser.py",
        side: "RIGHT",
        line: 2,
        startLine: null,
        headSha: HEAD,
        baseSha: BASE,
      },
      pendingReviewId: null,
      expectedHead: HEAD,
    });
    await waitFor(() => expect(host.write).toHaveBeenCalledTimes(2));
    signInAs("member-1");
    fail(lost());
    expect(await add).toEqual({ kind: "suppressed" });
    expect(pendingHoldsOf("member-1", PR)).toEqual([]);

    // Nor does a review the owner submitted just before signing out toast for the next user.
    signInAs("owner-1");
    await waitFor(() => expect(hostChangeRoute(SESSION, "submitReview")).not.toBeNull());
    let answerReview!: (value: unknown) => void;
    host.write.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          answerReview = resolve;
        }),
    );

    sendReviewOnHost({
      key: "owner-1|review",
      route: hostReviewRoute(SESSION, PR, "octo")!,
      prUrl: PR,
      event: "COMMENT",
      body: "Looks fine",
      headSha: "a".repeat(40),
      priorState: null,
      origin: null,
      queryClient: new QueryClient(),
    });
    await waitFor(() => expect(host.write).toHaveBeenCalledTimes(3));
    signInAs("member-1");
    answerReview({ object: "session.github.pr_review", pr_url: PR, event: "COMMENT" });

    await waitFor(() => expect(readReviewSubmit("owner-1|review").status.kind).toBe("idle"));
    expect(host.toast).not.toHaveBeenCalled();
  });

  it("keeps the owner's held change from the next user, unshown and uncheckable", async () => {
    const route = await ownerRoute();
    host.write.mockRejectedValueOnce(lost());
    const outcome = await sendPendingReviewChange(PR, route, {
      kind: "discard",
      reviewId: REVIEW_ID,
    });
    expect(outcome.kind).toBe("held");
    const [held] = pendingHoldsOf("owner-1", PR);

    signInAs("member-1");
    await waitFor(() => expect(hostChangeRoute(SESSION, "getPendingReview")).not.toBeNull());

    expect(pendingHoldsOf("member-1", PR)).toEqual([]);
    expect(pendingScopeCurrent(held!.sentAs, PR)).toBe(false);
    expect(await checkPendingHold(held!.id)).toBeNull();
    expect(host.read).not.toHaveBeenCalled();
  });
});
