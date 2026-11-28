// Pending-review changes through the session's host, for an editor and for the owner: each
// sent once as the step the caller's list serves, a draft with no review started first as its
// own step, and a lost answer held until Check again reads the pending review through the host.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const host = vi.hoisted(() => ({
  served: new Set<string>(),
  write: vi.fn(),
  read: vi.fn(),
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

import { PullRequestApiError } from "@/lib/pullRequestRpc";
import type { WorkspaceClient } from "@/lib/workspaceApi";
import { PullRequestMaybeSentError } from "@/lib/workspaceApi";
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

import { type HostPendingRoute, hostPendingRoute } from "./hostPendingRoute";
import type { DraftPlace } from "./pendingReview";
import {
  checkPendingHold,
  hostPendingChangeInFlight,
  type PendingChangeOutcome,
  type PendingReviewChange,
  pendingHoldsOf,
  resetPendingReviewChangesForTests,
  sendPendingReviewChange,
} from "./pendingReviewChanges";

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
// What next's levels give each caller through the host.
const EDITOR = [
  ...READS,
  "GetPendingReview",
  "StartPendingReview",
  "AddPendingReviewComment",
  "UpdatePendingReviewComment",
];
const OWNER = [
  ...EDITOR,
  "DeletePendingReviewComment",
  "SubmitPendingReview",
  "DiscardPendingReview",
];

const LINES: DraftPlace = {
  kind: "lines",
  path: "src/parser.py",
  side: "RIGHT",
  line: 4,
  startLine: null,
  headSha: HEAD,
  baseSha: BASE,
};

/** The page's route decided as `served`, and the host route it gives the PR. */
async function decide(served: readonly string[]): Promise<HostPendingRoute> {
  host.served = new Set(served);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  const view = renderHook(() => usePullRequestPageRoute(SESSION), { wrapper });
  await waitFor(() => expect(view.result.current).not.toBe("loading"));
  const route = hostPendingRoute(SESSION, PR);
  expect(route).not.toBeNull();
  return route!;
}

/** Each write's method and request, in order. */
async function sentWrites(): Promise<[string, unknown][]> {
  const sent: [string, unknown][] = [];
  const client = new Proxy(
    {},
    {
      get: (_target, method: string) => (request: unknown) => {
        sent.push([method, request]);
        return Promise.resolve({});
      },
    },
  ) as WorkspaceClient;
  // Each call records synchronously before it awaits, so the order is the calls' order.
  await Promise.all(host.write.mock.calls.map(([, rpc]) => rpc.call(client)));
  return sent;
}

const lineThread = (id: string, body: string) => ({
  ...draftThread("PRRT_new", [{ id, body }], { path: "src/parser.py", line: 4 }),
});
const addedThread = (body: string) => ({
  object: "session.github.pr_review_draft",
  pr_url: PR,
  review_id: REVIEW_ID,
  thread: lineThread("PRRC_new", body),
});
const started = {
  object: "session.github.pr_review_start",
  pr_url: PR,
  review_id: REVIEW_ID,
  commit_oid: HEAD,
};
const lost = () => new PullRequestMaybeSentError("The connection closed.");

const add = (pendingReviewId: string | null, body = "Why strip?"): PendingReviewChange => ({
  kind: "draft_add",
  body,
  place: LINES,
  pendingReviewId,
  expectedHead: HEAD,
});

const send = (route: HostPendingRoute, change: PendingReviewChange) =>
  sendPendingReviewChange(PR, route, change);

beforeEach(() => {
  host.write.mockReset();
  host.read.mockReset();
});

afterEach(() => {
  resetPendingReviewChangesForTests();
  resetPullRequestPageRoutesForTests();
});

describe("pending-review changes through the session's host", () => {
  it.each([
    ["an editor", EDITOR],
    ["the owner", OWNER],
  ])("%s starts a review, then adds the draft to it", async (_who, served) => {
    const route = await decide(served);
    host.write.mockResolvedValueOnce(started).mockResolvedValueOnce(addedThread("Why strip?"));

    const outcome = await send(route, add(null));

    expect(outcome.kind).toBe("done");
    expect(await sentWrites()).toEqual([
      ["startPendingReview", { prUrl: PR, commitOid: HEAD }],
      [
        "addPendingReviewComment",
        {
          prUrl: PR,
          body: "Why strip?",
          pendingReviewId: REVIEW_ID,
          expectedHeadOid: HEAD,
          anchor: {
            path: "src/parser.py",
            side: "RIGHT",
            line: 4,
            headSha: HEAD,
            baseSha: BASE,
          },
        },
      ],
    ]);
    // A review this send started has nothing in it yet: no read before the add.
    expect(host.read).not.toHaveBeenCalled();
  });

  it("adds to the review the page saw after reading what's already there", async () => {
    const route = await decide(EDITOR);
    host.read.mockResolvedValue(pendingAnswer());
    host.write.mockResolvedValueOnce(addedThread("Why strip?"));

    const outcome = await send(route, add(REVIEW_ID));

    expect(outcome.kind).toBe("done");
    expect((await sentWrites()).map(([method]) => method)).toEqual(["addPendingReviewComment"]);
    expect(host.read).toHaveBeenCalledTimes(1);
  });

  it("sends nothing when the pending review is now another one", async () => {
    const route = await decide(EDITOR);
    host.read.mockResolvedValue(pendingAnswer({ reviewId: "PRR_other" }));

    const outcome = await send(route, add(REVIEW_ID));

    expect(outcome).toMatchObject({ kind: "refused", code: "review_changed" });
    expect(host.write).not.toHaveBeenCalled();
  });

  it("leaves a started review to add to when the add is refused", async () => {
    const route = await decide(EDITOR);
    host.write
      .mockResolvedValueOnce(started)
      .mockRejectedValueOnce(new PullRequestApiError("That line isn't in the diff.", 400));

    const outcome = await send(route, add(null));

    expect(outcome).toEqual({
      kind: "partial",
      reviewId: REVIEW_ID,
      cause: null,
      message: "That line isn't in the diff.",
    });
    expect(pendingHoldsOf("", PR)).toEqual([]);
  });

  it("holds a lost start, and Check again finds the review it made", async () => {
    const route = await decide(EDITOR);
    host.write.mockRejectedValueOnce(lost());

    const outcome = await send(route, add(null));

    expect(outcome.kind).toBe("held");
    expect((await sentWrites()).map(([method]) => method)).toEqual(["startPendingReview"]);
    const [held] = pendingHoldsOf(heldOwner(outcome), PR);
    host.read.mockResolvedValue(pendingAnswer());
    expect(await checkPendingHold(held!.id)).toEqual({ kind: "started", reviewId: REVIEW_ID });
    // Nothing is sent again by a check.
    expect(host.write).toHaveBeenCalledTimes(1);
  });

  it("holds a lost add, and Check again finds the new draft", async () => {
    const route = await decide(EDITOR);
    host.read.mockResolvedValueOnce(pendingAnswer());
    host.write.mockRejectedValueOnce(lost());

    const outcome = await send(route, add(REVIEW_ID, "Why strip?"));

    expect(outcome.kind).toBe("held");
    const [held] = pendingHoldsOf(heldOwner(outcome), PR);
    host.read.mockResolvedValueOnce(
      pendingAnswer({ threads: [lineThread("PRRC_new", "Why strip?")] }),
    );
    expect(await checkPendingHold(held!.id)).toEqual({ kind: "landed", commentId: "PRRC_new" });
    expect(pendingHoldsOf(heldOwner(outcome), PR)).toEqual([]);
  });

  it("keeps a lost add held while the pending review doesn't show it", async () => {
    const route = await decide(EDITOR);
    host.read.mockResolvedValue(pendingAnswer());
    host.write.mockRejectedValueOnce(lost());

    const outcome = await send(route, add(REVIEW_ID));
    const [held] = pendingHoldsOf(heldOwner(outcome), PR);

    expect(await checkPendingHold(held!.id)).toEqual({ kind: "unsettled" });
    expect(pendingHoldsOf(heldOwner(outcome), PR)).toHaveLength(1);
  });

  it("refuses an editor's discard and draft delete without sending anything", async () => {
    const route = await decide(EDITOR);

    const outcomes = await Promise.all(
      (
        [
          { kind: "discard", reviewId: REVIEW_ID },
          { kind: "draft_delete", commentId: "PRRC_1", reviewId: REVIEW_ID },
        ] as PendingReviewChange[]
      ).map((change) => send(route, change)),
    );
    for (const outcome of outcomes) {
      expect(outcome).toMatchObject({ kind: "refused", code: "pending_reviews_unavailable" });
    }
    expect(host.write).not.toHaveBeenCalled();
  });

  it("lets an editor edit a draft, against the text it started from", async () => {
    const route = await decide(EDITOR);
    host.write.mockResolvedValueOnce({
      object: "session.github.pr_comment_edit",
      comment_id: "PRRC_1",
      body: "Better",
    });

    const outcome = await send(route, {
      kind: "draft_edit",
      commentId: "PRRC_1",
      reviewId: REVIEW_ID,
      body: "Better",
      expected: "Good",
    });

    expect(outcome.kind).toBe("done");
    const [[method, request]] = await sentWrites();
    expect(method).toBe("updatePendingReviewComment");
    expect(request).toMatchObject({
      prUrl: PR,
      commentNodeId: "PRRC_1",
      body: "Better",
      pendingReviewId: REVIEW_ID,
    });
    expect((request as { expectedBodySha256: string }).expectedBodySha256).toMatch(
      /^[0-9a-f]{64}$/,
    );
  });

  it("lets the owner delete a draft and discard the review", async () => {
    const route = await decide(OWNER);
    host.write
      .mockResolvedValueOnce({ object: "session.github.pr_comment_delete", comment_id: "PRRC_1" })
      .mockResolvedValueOnce({ object: "session.github.pr_review_discard", review_id: REVIEW_ID });

    expect(
      (await send(route, { kind: "draft_delete", commentId: "PRRC_1", reviewId: REVIEW_ID })).kind,
    ).toBe("done");
    expect((await send(route, { kind: "discard", reviewId: REVIEW_ID })).kind).toBe("done");
    expect(await sentWrites()).toEqual([
      [
        "deletePendingReviewComment",
        { prUrl: PR, commentNodeId: "PRRC_1", pendingReviewId: REVIEW_ID },
      ],
      ["discardPendingReview", { prUrl: PR, pendingReviewId: REVIEW_ID }],
    ]);
  });

  it("settles a lost discard only while the review is still pending", async () => {
    const route = await decide(OWNER);
    host.write.mockRejectedValue(lost());

    const outcome = await send(route, { kind: "discard", reviewId: REVIEW_ID });
    const [held] = pendingHoldsOf(heldOwner(outcome), PR);

    host.read.mockResolvedValueOnce(noReviewAnswer());
    expect(await checkPendingHold(held!.id)).toEqual({ kind: "unsettled" });
    host.read.mockResolvedValueOnce(pendingAnswer());
    expect(await checkPendingHold(held!.id)).toEqual({ kind: "not_discarded" });
  });

  it("sends one change per pull request at a time", async () => {
    const route = await decide(OWNER);
    let answer: (value: unknown) => void = () => {};
    host.write.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
    );

    const first = send(route, { kind: "discard", reviewId: REVIEW_ID });
    await waitFor(() => expect(hostPendingChangeInFlight(PR)).toBe(true));
    expect(await send(route, add(REVIEW_ID))).toMatchObject({ kind: "refused", code: "busy" });

    answer({ object: "session.github.pr_review_discard", review_id: REVIEW_ID });
    expect((await first).kind).toBe("done");
    expect(hostPendingChangeInFlight(PR)).toBe(false);
    expect(host.write).toHaveBeenCalledTimes(1);
  });

  it("reports an answer that came after the route changed as moved", async () => {
    const route = await decide(OWNER);
    host.write.mockImplementationOnce(async () => {
      resetPullRequestPageRoutesForTests();
      return { object: "session.github.pr_review_discard", review_id: REVIEW_ID };
    });

    const outcome = await send(route, { kind: "discard", reviewId: REVIEW_ID });

    expect(outcome).toMatchObject({ kind: "moved", settled: "done" });
  });
});

/** The workspace user a held outcome's hold belongs to. */
function heldOwner(outcome: PendingChangeOutcome): string {
  if (outcome.kind !== "held") throw new Error(`expected a hold, got ${outcome.kind}`);
  return outcome.hold.owner;
}
