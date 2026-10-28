import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { PropsWithChildren } from "react";
import type { ReviewThreadsPage } from "@/lib/pullRequestReviewThreads";
import { usePullRequestReviewThreads } from "./usePullRequestReviewThreads";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const first: ReviewThreadsPage = {
  head_sha: "head",
  base_sha: "base",
  has_more: true,
  next_cursor: "threads-2",
  data: [
    {
      id: "thread",
      path: "app.py",
      side: "RIGHT",
      line: 2,
      start_line: null,
      subject_type: "LINE",
      resolved: false,
      outdated: false,
      can_reply: true,
      diff_hunk: "",
      comments: {
        data: [{ id: "root", author: "alice", body: "Why?", created_at: null, url: null }],
        has_more: true,
        next_cursor: "replies-2",
      },
    },
  ],
};

it("preserves loaded reply cursors and canonical replies through polls and omitted thread data", async () => {
  const client = new QueryClient();
  const fetch = vi.fn().mockResolvedValue(
    new Response(
      JSON.stringify({
        data: [{ id: "reply", author: "bob", body: "Because", created_at: null, url: null }],
        has_more: false,
        next_cursor: null,
      }),
    ),
  );
  vi.stubGlobal("fetch", fetch);
  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { result, rerender } = renderHook(
    ({ page }: { page?: ReviewThreadsPage }) =>
      usePullRequestReviewThreads("session", "pr", "account", "base:head", page),
    { wrapper, initialProps: { page: first } as { page?: ReviewThreadsPage } },
  );
  expect(fetch).not.toHaveBeenCalled();
  act(() => result.current.loadComments(first.data[0]));
  await waitFor(() => expect(result.current.page?.data[0].comments.data).toHaveLength(2));
  rerender({ page: structuredClone(first) });
  await waitFor(() => expect(result.current.historyStale).toBe(true));
  expect(result.current.page?.data[0].comments.next_cursor).toBeNull();
  expect(result.current.page?.data[0].comments.has_more).toBe(false);
  rerender({ page: undefined });
  expect(result.current.page?.data[0].comments.data).toHaveLength(2);
  rerender({ page: { ...first, data: [], warning: "Could not load conversations." } });
  await waitFor(() => expect(result.current.page?.warning).toBe("Could not load conversations."));
  expect(result.current.page?.data[0].comments.data).toHaveLength(2);
  expect(fetch).toHaveBeenCalledTimes(1);
});

it("keeps late pagination results with the original PR and isolates revisions and accounts", async () => {
  const client = new QueryClient();
  let respond!: (response: Response) => void;
  vi.stubGlobal(
    "fetch",
    vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          respond = resolve;
        }),
    ),
  );
  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { result, rerender } = renderHook(
    ({ url, account, revision, page }) =>
      usePullRequestReviewThreads("session", url, account, revision, page),
    {
      wrapper,
      initialProps: {
        url: "pr",
        account: "alice",
        revision: "base:head",
        page: first as ReviewThreadsPage | undefined,
      },
    },
  );
  act(() => result.current.loadMore());
  await waitFor(() => expect(respond).toBeDefined());
  rerender({ url: "other", account: "bob", revision: "new", page: undefined });
  await act(async () =>
    respond(
      new Response(
        JSON.stringify({
          ...first,
          data: [{ ...first.data[0], id: "extra" }],
          has_more: false,
          next_cursor: null,
        }),
      ),
    ),
  );
  expect(result.current.page).toBeUndefined();
  rerender({ url: "pr", account: "alice", revision: "base:head", page: first });
  await waitFor(() => expect(result.current.page?.data).toHaveLength(2));
  expect(result.current.page?.has_more).toBe(false);
});
