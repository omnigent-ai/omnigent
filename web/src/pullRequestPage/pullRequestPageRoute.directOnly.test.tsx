// The new PR page where only the desktop's direct route reaches the session's host, as on a
// server without the PR methods: the host's socket serves the page's reads and nothing else,
// so the page shows on that route and offers no change, lookup or owner-only read.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  enabledWorkspaceRoutes: vi.fn(),
  servingRoutes: vi.fn(),
}));
vi.mock("@/lib/workspaceApi", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkspaceApiModule>()),
  ...api,
}));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => ({ features: {} }) }));

import type * as WorkspaceApiModule from "@/lib/workspaceApi";
import type { Method } from "@/lib/workspaceApi";
import {
  accountSwitchingOffered,
  normalizePullRequestInfo,
  pullRequestCommentControls,
  type PullRequestInfo,
} from "@/newPullRequestPage/hooks/usePullRequests";
import {
  hostChangeRoute,
  resetPullRequestPageRoutesForTests,
  usePullRequestMethodServed,
  usePullRequestPageRoute,
} from "./pullRequestPageRoute";

const SESSION = "conv_1";
// What the host's socket lists: the page's reads, each for one PR the session recorded.
const SOCKET_METHODS = [
  "GetPullRequest",
  "ListPullRequestFiles",
  "GetPullRequestDiff",
  "ListPullRequestCommits",
  "GetCommitDiff",
  "ComparePullRequestRange",
  "ListReviewThreads",
  "ListReviewThreadComments",
];
// Each method below gates a control or read the page offers only where its route serves it.
const NOT_ON_THE_SOCKET: Method[] = [
  "getPendingReview",
  "getMergeOptions",
  "listLabels",
  "listReviewerCandidates",
  "searchCompletions",
  "lookupPullRequest",
  "getPullRequestListStats",
  "searchReviewers",
  "createComment",
  "replyToReviewThread",
  "submitReview",
  "submitReviewVerdict",
  "addReaction",
  "editComment",
  "setReviewThreadResolved",
  "mergePullRequest",
  "setPullRequestState",
  "setPullRequestAccount",
];

function renderPage() {
  const client = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(
    () => ({
      route: usePullRequestPageRoute(SESSION),
      listsCommits: usePullRequestMethodServed(SESSION, "listPullRequestCommits"),
      diffsCommits: usePullRequestMethodServed(SESSION, "getCommitDiff"),
      comparesRange: usePullRequestMethodServed(SESSION, "comparePullRequestRange"),
      switchesAccount: usePullRequestMethodServed(SESSION, "setPullRequestAccount"),
      unserved: NOT_ON_THE_SOCKET.filter((method) => usePullRequestMethodServed(SESSION, method)),
    }),
    { wrapper },
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  resetPullRequestPageRoutesForTests();
  api.enabledWorkspaceRoutes.mockReturnValue(["direct"]);
  api.servingRoutes.mockResolvedValue({
    serving: [{ route: "direct", key: `direct:${SESSION}:`, served: new Set(SOCKET_METHODS) }],
    failures: [],
  });
});

describe("the new PR page on a direct-only route", () => {
  it("reads through the socket, Commits tab included", async () => {
    const view = renderPage();

    await waitFor(() => expect(view.result.current.route).not.toBe("loading"));

    expect(view.result.current.route).toMatchObject({ route: "direct" });
    expect(view.result.current.listsCommits).toBe(true);
    expect(view.result.current.diffsCommits).toBe(true);
    expect(view.result.current.comparesRange).toBe(true);
  });

  it("offers no change, lookup, pending review or owner-only read", async () => {
    const view = renderPage();

    await waitFor(() => expect(view.result.current.route).not.toBe("loading"));

    expect(view.result.current.unserved).toEqual([]);
    for (const method of NOT_ON_THE_SOCKET) {
      expect(hostChangeRoute(SESSION, method)).toBeNull();
    }
  });

  it("shows no comment box for the read-only payload the socket sends", () => {
    // The socket serves every code host read-only, so the host clears each write capability.
    const info = normalizePullRequestInfo({
      provider: "github",
      read_only: true,
      capabilities: {
        comment_posting: false,
        review_thread_replies: false,
        review_thread_creation: false,
        merging: false,
        label_editing: false,
        reviewer_editing: false,
      },
      auth: { authenticated: true, selected_account: "octocat" },
    } as unknown as PullRequestInfo);

    expect(pullRequestCommentControls(info)).toMatchObject({
      comment: false,
      reply: false,
      createThread: false,
    });
  });

  it("shows no account picker, which saves through a change the socket doesn't serve", async () => {
    const view = renderPage();
    await waitFor(() => expect(view.result.current.route).not.toBe("loading"));
    // The host still says the provider switches accounts; only the route decides the change.
    const info = normalizePullRequestInfo({
      provider: "github",
      capabilities: { account_switching: true },
      auth: {
        authenticated: true,
        selected_account: "octocat",
        accounts: [{ login: "octocat" }, { login: "hubot" }],
      },
    } as unknown as PullRequestInfo);

    expect(view.result.current.switchesAccount).toBe(false);
    expect(accountSwitchingOffered(info, view.result.current.switchesAccount)).toBe(false);
    expect(accountSwitchingOffered(info, true)).toBe(true);
  });
});
