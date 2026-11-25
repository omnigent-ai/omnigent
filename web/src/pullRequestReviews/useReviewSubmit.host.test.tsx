// Reviews without this Mac's shell: where the session's host lists SubmitReview the Review menu
// sends a comment review through it, and where it lists SubmitReviewVerdict too (the owner's),
// approve and request changes. A lost answer holds the review until Check Again reads the PR.

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

import { resetDesktopPullRequestWritesForTests } from "@/lib/desktopPullRequestWrites";
import { PullRequestApiError } from "@/lib/pullRequestRpc";
import type { WorkspaceClient } from "@/lib/workspaceApi";
import { PullRequestMaybeSentError } from "@/lib/workspaceApi";
import {
  resetPullRequestPageRoutesForTests,
  usePullRequestPageRoute,
} from "@/pullRequestPage/pullRequestPageRoute";

import { draftThread, pendingAnswer, REVIEW_ID } from "@/test/pendingReviewAnswers";

import { resetHostReviewsForTests } from "./hostReviews";
import { resetPendingReviewChangesForTests } from "./pendingReviewChanges";
import { reviewCommandRows } from "./reviewCommands";
import { resetReviewSubmitForTests, type ReviewEvent } from "./reviewSubmit";
import {
  PENDING_SUBMIT_OWNERS,
  pendingFinishUnavailableReason,
  type ReviewPullRequest,
  useReviewOffer,
  useReviewSubmit,
} from "./useReviewSubmit";

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
// What next's levels give each caller: a comment review for an editor, verdicts for the owner.
const EDITOR = [...READS, "SubmitReview"];
const OWNER = [...EDITOR, "SubmitReviewVerdict"];

/** The Review menu's controller on a page whose route was decided as `served`. */
async function renderMenu(
  served: readonly string[],
  {
    account = "octo",
    reviewState = null,
  }: { account?: string | null; reviewState?: string | null } = {},
) {
  host.served = new Set(served);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  const pr: ReviewPullRequest = {
    url: PR,
    number: 42,
    state: "OPEN",
    author: "someone-else",
    headSha: HEAD,
    hostReviewState: reviewState,
  };
  const view = renderHook(
    () => {
      const route = usePullRequestPageRoute(SESSION);
      const offer = useReviewOffer(pr, "GitHub", { conversationId: SESSION, account });
      return { route, offer, review: useReviewSubmit({ conversationId: SESSION, pr, offer }) };
    },
    { wrapper },
  );
  await waitFor(() => expect(view.result.current.route).not.toBe("loading"));
  view.rerender();
  return view;
}

type View = Awaited<ReturnType<typeof renderMenu>>;

function choose(view: View, event: ReviewEvent, body: string) {
  act(() => view.result.current.review.setEvent(event));
  act(() => view.result.current.review.setBody(body));
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

const answer = (event: ReviewEvent) => ({
  object: "session.github.pr_review",
  pr_url: PR,
  event,
});

const reviewers = (state: string) => ({
  available: true,
  pr: { url: PR, reviewers: [{ login: "octo", kind: "user", state }] },
});

beforeEach(() => {
  host.write.mockReset();
  host.read.mockReset();
});
afterEach(() => {
  resetPullRequestPageRoutesForTests();
  resetDesktopPullRequestWritesForTests();
  resetReviewSubmitForTests();
  resetHostReviewsForTests();
  resetPendingReviewChangesForTests();
});

describe("reviews through the session's host, as the owner", () => {
  it("offers every verdict and approves once, on the head shown, as the host's account", async () => {
    host.write.mockResolvedValue(answer("APPROVE"));
    const view = await renderMenu(OWNER);
    expect(view.result.current.offer).toMatchObject({ kind: "host", verdicts: true });
    expect(view.result.current.review.events).toEqual(["COMMENT", "APPROVE", "REQUEST_CHANGES"]);
    expect(view.result.current.review.viewer).toBe("octo");
    choose(view, "APPROVE", "");
    act(() => view.result.current.review.submit());
    await waitFor(() => expect(view.result.current.review.entry.status.kind).toBe("idle"));
    expect(host.write).toHaveBeenCalledTimes(1);
    await expect(sent()).resolves.toEqual([
      ["submitReviewVerdict", { prUrl: PR, event: "APPROVE", body: "", commitOid: HEAD }],
    ]);
  });

  it("sends a comment review with SubmitReview", async () => {
    host.write.mockResolvedValue(answer("COMMENT"));
    const view = await renderMenu(OWNER);
    choose(view, "COMMENT", "Looks fine");
    act(() => view.result.current.review.submit());
    await waitFor(() => expect(host.write).toHaveBeenCalledTimes(1));
    await expect(sent()).resolves.toEqual([
      ["submitReview", { prUrl: PR, event: "COMMENT", body: "Looks fine", commitOid: HEAD }],
    ]);
  });

  it("offers no verdicts where the host names no account to check them against", async () => {
    const view = await renderMenu(OWNER, { account: null });
    expect(view.result.current.offer).toMatchObject({ kind: "host", verdicts: false });
    expect(view.result.current.review.events).toEqual(["COMMENT"]);
  });

  it("shows GitHub's refusal and keeps the review to edit", async () => {
    host.write.mockRejectedValue(
      new PullRequestApiError("Can not approve your own pull request", 400),
    );
    const view = await renderMenu(OWNER);
    choose(view, "APPROVE", "");
    act(() => view.result.current.review.submit());
    await waitFor(() => expect(view.result.current.review.entry.status.kind).toBe("refused"));
    expect(view.result.current.review.entry.status).toMatchObject({
      message: "Can not approve your own pull request",
    });
  });
});

describe("reviews through the session's host, as an editor", () => {
  it("offers a comment review only, and never sends a verdict", async () => {
    host.write.mockResolvedValue(answer("COMMENT"));
    const view = await renderMenu(EDITOR);
    expect(view.result.current.offer).toMatchObject({ kind: "host", verdicts: false });
    expect(view.result.current.review.events).toEqual(["COMMENT"]);
    act(() => view.result.current.review.setEvent("APPROVE"));
    expect(view.result.current.review.entry.event).toBe("COMMENT");
    choose(view, "COMMENT", "A note");
    act(() => view.result.current.review.submit());
    await waitFor(() => expect(host.write).toHaveBeenCalledTimes(1));
    await expect(sent()).resolves.toEqual([
      ["submitReview", { prUrl: PR, event: "COMMENT", body: "A note", commitOid: HEAD }],
    ]);
  });

  it("lists no verdicts in ⌘K, where the owner's list has all three", async () => {
    const rowIds = (view: View) =>
      reviewCommandRows(view.result.current.offer, view.result.current.review, {
        openMenu: () => {},
        openOnGitHub: () => {},
      }).map((row) => row.id);
    expect(rowIds(await renderMenu(EDITOR))).toEqual(["submit"]);
    resetPullRequestPageRoutesForTests();
    expect(rowIds(await renderMenu(OWNER))).toEqual(["submit", "approve", "request-changes"]);
  });

  it("links to GitHub where the host doesn't take reviews", async () => {
    const view = await renderMenu(READS);
    expect(view.result.current.offer).toMatchObject({ kind: "link" });
  });
});

describe("a host review whose answer was lost", () => {
  async function lostApproval(reviewState: string | null) {
    host.write.mockRejectedValue(new PullRequestMaybeSentError("lost"));
    const view = await renderMenu(OWNER, { reviewState });
    choose(view, "APPROVE", "");
    act(() => view.result.current.review.submit());
    await waitFor(() => expect(view.result.current.review.entry.status.kind).toBe("uncertain"));
    return view;
  }

  it("is held, never sent again, and settles once the host shows the approval", async () => {
    const view = await lostApproval("COMMENTED");
    act(() => view.result.current.review.submit());
    expect(host.write).toHaveBeenCalledTimes(1);
    host.read.mockResolvedValue(reviewers("APPROVED"));
    act(() => view.result.current.review.checkAgain());
    await waitFor(() => expect(view.result.current.review.entry.status.kind).toBe("idle"));
    expect(host.read.mock.calls[0]?.slice(0, 2)).toEqual([SESSION, "getPullRequest"]);
    expect(host.write).toHaveBeenCalledTimes(1);
  });

  it("stays held when the account had approved before, as that could be the one shown", async () => {
    const view = await lostApproval("APPROVED");
    host.read.mockResolvedValue(reviewers("APPROVED"));
    act(() => view.result.current.review.checkAgain());
    await waitFor(() =>
      expect(view.result.current.review.entry.status).toMatchObject({ checked: "ambiguous" }),
    );
  });

  it("stays held when the host doesn't show it, and when the read fails", async () => {
    const view = await lostApproval(null);
    host.read.mockResolvedValue(reviewers("COMMENTED"));
    act(() => view.result.current.review.checkAgain());
    await waitFor(() =>
      expect(view.result.current.review.entry.status).toMatchObject({ checked: "not_found" }),
    );
    host.read.mockRejectedValue(new Error("offline"));
    act(() => view.result.current.review.checkAgain());
    await waitFor(() =>
      expect(view.result.current.review.entry.status).toMatchObject({ checked: "unreadable" }),
    );
    expect(view.result.current.review.keepAsDraft.available).toBe(true);
  });
});

describe("finishing a pending review through the session's host", () => {
  const PENDING_READS = ["GetPendingReview", "StartPendingReview", "AddPendingReviewComment"];
  const pending = () =>
    pendingAnswer({
      threads: [draftThread("PRRT_1", [{ id: "PRRC_1", body: "Why strip?" }])],
      commit: HEAD,
      head: HEAD,
    });

  /** The menu with a pending review read through the host; the reviewers read answers too. */
  async function renderPendingMenu(served: readonly string[]) {
    host.read.mockImplementation(async (_session: string, method: string) =>
      method === "getPendingReview" ? pending() : reviewers("COMMENTED"),
    );
    const view = await renderMenu(served);
    await waitFor(() => expect(view.result.current.review.finish?.reviewId).toBe(REVIEW_ID));
    return view;
  }

  it("lets the owner submit the drafts as one review, naming the review it read", async () => {
    host.write.mockResolvedValue({
      ...answer("COMMENT"),
      state: "COMMENTED",
      review: { id: REVIEW_ID, state: "COMMENTED", body: "Two notes", commit_oid: HEAD },
    });
    const view = await renderPendingMenu([...OWNER, ...PENDING_READS, "SubmitPendingReview"]);
    expect(view.result.current.review.finish).toMatchObject({ submittable: true });
    choose(view, "COMMENT", "Two notes");
    act(() => view.result.current.review.submit());
    await waitFor(() => expect(host.write).toHaveBeenCalledTimes(1));
    const [[method, request]] = await sent();
    expect(method).toBe("submitPendingReview");
    expect(request).toMatchObject({ prUrl: PR, event: "COMMENT", pendingReviewId: REVIEW_ID });
  });

  it("tells an editor the submit is the owner's and sends nothing", async () => {
    const view = await renderPendingMenu([...EDITOR, ...PENDING_READS]);
    const { finish } = view.result.current.review;
    expect(finish).toMatchObject({ submittable: false, discardable: false });
    expect(
      pendingFinishUnavailableReason(view.result.current.review.entry, finish!, {
        viewer: "octo",
        author: "someone-else",
        headSha: HEAD,
      }),
    ).toBe(PENDING_SUBMIT_OWNERS);
    choose(view, "COMMENT", "Two notes");
    act(() => view.result.current.review.submit());
    expect(host.write).not.toHaveBeenCalled();
  });
});
