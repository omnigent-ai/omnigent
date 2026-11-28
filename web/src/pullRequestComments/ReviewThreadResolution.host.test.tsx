// Resolve and Unresolve without this Mac's shell: where the session's host serves the method,
// the thread offers the change and sends it through the host, once; where it doesn't, nothing.

import type * as SonnerModule from "sonner";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { toast } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("sonner", async (importOriginal) => ({
  ...(await importOriginal<typeof SonnerModule>()),
  toast: Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    dismiss: vi.fn(),
  }),
}));
const host = vi.hoisted(() => ({
  serves: true,
  write: vi.fn(),
}));
vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  usePullRequestMethodServed: (_sessionId: string, method: string) =>
    host.serves && method === "setReviewThreadResolved",
  // The route the write goes along, decided for the signed-in user.
  hostChangeRoute: (sessionId: string, method: string) =>
    host.serves && method === "setReviewThreadResolved"
      ? { conversationId: sessionId, key: `server:${sessionId}` }
      : null,
}));
vi.mock("@/lib/pullRequestRpc", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  writePullRequestRpc: host.write,
}));

import { TooltipProvider } from "@/components/ui/tooltip";
import { PullRequestApiError } from "@/lib/pullRequestRpc";
import type { ReviewThread } from "@/lib/pullRequestReviewThreads";
import type { WorkspaceClient } from "@/lib/workspaceApi";
import {
  PullRequestReviewThread,
  type ReviewThreadContext,
} from "@/newPullRequestPage/shell/PullRequestReviewThread";

import { resetReviewThreadResolutionsForTests } from "./useReviewThreadResolution";

const PR = "https://github.com/acme/app/pull/42";
const THREAD: ReviewThread = {
  id: "PRRT_1",
  path: "src/app.js",
  side: "RIGHT",
  line: 3,
  start_line: null,
  subject_type: "LINE",
  resolved: false,
  outdated: false,
  can_reply: true,
  can_resolve: true,
  can_unresolve: true,
  diff_hunk: "@@ -1 +1 @@",
  comments: {
    data: [
      {
        id: "PRRC_1",
        author: "bob",
        body: "Rename this",
        created_at: "2026-11-05T10:00:00Z",
        url: `${PR}#discussion_r1`,
      },
    ],
    has_more: false,
    next_cursor: null,
  },
};
const CONTEXT: ReviewThreadContext = {
  conversationId: "conv_1",
  prUrl: PR,
  account: "alice",
  canReply: true,
  loadComments: vi.fn(),
  loadingComments: null,
};
const hostAnswer = (resolved: boolean) => ({
  object: "session.github.review_thread_resolution",
  pr_url: PR,
  thread_id: THREAD.id,
  resolved,
});

/** What each write sent to the host's method. */
async function sentRequests(): Promise<unknown[]> {
  const requests: unknown[] = [];
  const workspace = {
    setReviewThreadResolved: async (request: unknown) => {
      requests.push(request);
      return { payload: {} };
    },
  } as unknown as WorkspaceClient;
  await Promise.all(host.write.mock.calls.map(([, rpc]) => rpc.call(workspace)));
  return requests;
}

function showThread(thread: ReviewThread = THREAD) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <PullRequestReviewThread thread={thread} context={CONTEXT} />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}
const diffThread = () => screen.getByRole("region", { name: /^Review conversation/ });

beforeEach(() => {
  host.serves = true;
  host.write.mockReset();
});
afterEach(() => {
  cleanup();
  vi.mocked(toast.error).mockClear();
  resetReviewThreadResolutionsForTests();
});

describe("resolving through the session's host", () => {
  it("offers Resolve and sends it once through the host's method", async () => {
    let answer: (value: unknown) => void = () => {};
    host.write.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
    );
    showThread();
    fireEvent.click(within(diffThread()).getByRole("button", { name: "Resolve Conversation" }));
    await waitFor(() => expect(host.write).toHaveBeenCalledTimes(1));
    expect(host.write.mock.calls[0]?.[0]).toBe("conv_1");
    expect(host.write.mock.calls[0]?.[1]).toMatchObject({ method: "setReviewThreadResolved" });
    await expect(sentRequests()).resolves.toEqual([
      { prUrl: PR, threadId: THREAD.id, resolved: true },
    ]);
    fireEvent.click(within(diffThread()).getByRole("button", { name: "Resolving…" }));
    await act(async () => answer(hostAnswer(true)));
    await waitFor(() =>
      expect(
        within(diffThread()).getByRole("button", { name: "Resolve Conversation" }),
      ).toBeEnabled(),
    );
    expect(host.write).toHaveBeenCalledTimes(1);
    expect(toast.error).not.toHaveBeenCalled();
  });

  it("sends Unresolve for a resolved conversation", async () => {
    host.write.mockResolvedValue(hostAnswer(false));
    showThread({ ...THREAD, resolved: true });
    const menu = within(diffThread()).getByRole("button", { name: "Conversation actions" });
    fireEvent.pointerDown(menu, { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Unresolve Conversation" }));
    await waitFor(() => expect(host.write).toHaveBeenCalledTimes(1));
    await expect(sentRequests()).resolves.toEqual([
      { prUrl: PR, threadId: THREAD.id, resolved: false },
    ]);
  });

  it("tells the host's refusal", async () => {
    host.write.mockRejectedValue(new PullRequestApiError("You can't resolve this.", 403));
    showThread();
    fireEvent.click(within(diffThread()).getByRole("button", { name: "Resolve Conversation" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("You can't resolve this."));
  });

  it("says GitHub may have made it when the host's answer can't be read", async () => {
    host.write.mockResolvedValue({ object: "something.else" });
    showThread();
    fireEvent.click(within(diffThread()).getByRole("button", { name: "Resolve Conversation" }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(expect.stringMatching(/may have made this change/)),
    );
  });

  it("offers nothing where the host doesn't serve the method", () => {
    host.serves = false;
    showThread();
    expect(screen.queryByRole("button", { name: "Resolve Conversation" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Conversation actions" })).toBeNull();
  });
});
