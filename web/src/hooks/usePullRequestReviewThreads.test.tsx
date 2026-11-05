import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { PropsWithChildren } from "react";
import { appendReviewReply, type ReviewThreadsPage } from "@/lib/pullRequestReviewThreads";
import { usePullRequestReviewThreads } from "./usePullRequestReviewThreads";
import { usePullRequestReviewThreadComposer } from "./usePullRequests";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { clearLocalReviewContexts, usePendingLocalReviewContexts } from "@/lib/localReviewContext";
import type { Comment } from "./useComments";

afterEach(() => {
  clearLocalReviewContexts("session");
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

it("retains a new range draft, requires reselection after a revision change, and isolates a late write", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(["github-review-threads", "session", "pr", "account", "base:head"], first);
  client.setQueryData(["github-review-threads", "session", "pr", "account", "base:changed"], {
    ...first,
    head_sha: "changed",
  });
  let respond!: (response: Response) => void;
  const fetch = vi.fn<(...args: Parameters<typeof globalThis.fetch>) => Promise<Response>>(
    () =>
      new Promise<Response>((resolve) => {
        respond = resolve;
      }),
  );
  vi.stubGlobal("fetch", fetch);
  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { result, rerender } = renderHook(
    ({ url, head }) => ({
      composer: usePullRequestReviewThreadComposer("session", url, "app.py", head, "base"),
      threads: usePullRequestReviewThreads("session", url, "account", `base:${head}`, false),
    }),
    { wrapper, initialProps: { url: "pr", head: "head", page: first } },
  );
  act(() => result.current.composer.selectLines({ side: "LEFT", line: 7, start_line: 5 }));
  act(() => result.current.composer.setBody("**New conversation**"));
  rerender({ url: "other", head: "head", page: first });
  expect(result.current.composer.draft.body).toBe("");
  rerender({ url: "pr", head: "changed", page: { ...first, head_sha: "changed" } });
  expect(result.current.composer.draft.body).toBe("**New conversation**");
  expect(result.current.composer.stale).toBe(true);
  act(() => result.current.composer.post());
  expect(fetch).not.toHaveBeenCalled();
  act(() => result.current.composer.selectLines({ side: "RIGHT", line: 2, start_line: null }));
  act(() => {
    result.current.composer.post();
    result.current.composer.post();
  });
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  expect(JSON.parse(fetch.mock.calls[0][1]!.body as string)).toEqual({
    pr_url: "pr",
    body: "**New conversation**",
    anchor: {
      path: "app.py",
      side: "RIGHT",
      line: 2,
      start_line: null,
      head_sha: "changed",
      base_sha: "base",
    },
  });
  rerender({ url: "other", head: "head", page: first });
  act(() => result.current.composer.setBody("Other draft"));
  await waitFor(() => expect(result.current.composer.draft.body).toBe("Other draft"));
  const created = { ...first.data[0], id: "created" };
  await act(async () => respond(new Response(JSON.stringify({ thread: created }))));
  expect(result.current.composer.draft.body).toBe("Other draft");
  rerender({ url: "pr", head: "changed", page: { ...first, head_sha: "changed" } });
  await waitFor(() => expect(result.current.composer.draft.anchor).toBeUndefined());
  expect(result.current.threads.page?.data.map(({ id }) => id)).toEqual(["thread", "created"]);
  expect(result.current.threads.page?.next_cursor).toBe("threads-2");
});

it("keeps text and coordinates after an uncertain creation without retrying", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const fetch = vi.fn().mockRejectedValue(new TypeError("Network error"));
  vi.stubGlobal("fetch", fetch);
  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { result } = renderHook(
    () => usePullRequestReviewThreadComposer("session", "pr", "app.py", "head", "base"),
    { wrapper },
  );
  act(() => result.current.selectLines({ side: "RIGHT", line: 2, start_line: null }));
  act(() => result.current.setBody("Keep me"));
  act(() => result.current.post());
  await waitFor(() => expect(result.current.draft.error).toMatch(/Check GitHub before retrying/));
  expect(result.current.draft.body).toBe("Keep me");
  expect(result.current.draft.anchor?.line).toBe(2);
  expect(fetch).toHaveBeenCalledTimes(1);
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

it("preserves paginated history through polls and retains it when the read is incomplete", async () => {
  const client = new QueryClient();
  const fetch = vi.fn().mockImplementation(
    async (url: string) =>
      new Response(
        JSON.stringify(
          url.includes("/comments?")
            ? {
                data: [
                  { id: "reply", author: "bob", body: "Because", created_at: null, url: null },
                ],
                has_more: false,
                next_cursor: null,
              }
            : first,
        ),
      ),
  );
  vi.stubGlobal("fetch", fetch);
  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { result } = renderHook(
    () => usePullRequestReviewThreads("session", "pr", "account", "base:head", true),
    { wrapper },
  );
  await waitFor(() => expect(result.current.page?.data).toHaveLength(1));
  act(() => result.current.loadComments(first.data[0]));
  await waitFor(() => expect(result.current.page?.data[0].comments.data).toHaveLength(2));
  await act(async () => client.invalidateQueries({ queryKey: ["github-review-threads"] }));
  await waitFor(() => expect(result.current.historyStale).toBe(true));
  expect(result.current.page?.data[0].comments.next_cursor).toBeNull();
  expect(result.current.page?.data[0].comments.has_more).toBe(false);
  fetch.mockImplementation(
    async () =>
      new Response(
        JSON.stringify({ ...first, data: [], warning: "Could not load conversations." }),
      ),
  );
  await act(async () => client.invalidateQueries({ queryKey: ["github-review-threads"] }));
  await waitFor(() => expect(result.current.page?.warning).toBe("Could not load conversations."));
  expect(result.current.page?.data[0].comments.data).toHaveLength(2);
  fetch.mockImplementation(async () => new Response(JSON.stringify(first)));
  act(() => result.current.refreshHistory());
  await waitFor(() => expect(result.current.page?.data[0].comments.data).toHaveLength(1));
  expect(result.current.historyStale).toBe(false);
});

it("keeps late pagination results with the original PR and isolates revisions and accounts", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(["github-review-threads", "session", "pr", "alice", "base:head"], first);
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
    ({ url, account, revision }) =>
      usePullRequestReviewThreads("session", url, account, revision, false),
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
  expect(result.current.isFetching).toBe(false);
  act(() => result.current.loadMore());
  await waitFor(() => expect(respond).toBeDefined());
  await waitFor(() => expect(result.current.isFetching).toBe(true));
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
  expect(result.current.isFetching).toBe(false);
});

it("fetches on opening Diff, polls only while active, and removes deleted comments after confirmation", async () => {
  vi.useFakeTimers();
  const client = new QueryClient();
  const key = ["github-review-threads", "session", "pr", "account", "base:head"];
  let serverPage = first;
  const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify(serverPage)));
  vi.stubGlobal("fetch", fetch);
  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { result, rerender } = renderHook(
    ({ enabled }) => usePullRequestReviewThreads("session", "pr", "account", "base:head", enabled),
    { wrapper, initialProps: { enabled: false } },
  );
  expect(fetch).not.toHaveBeenCalled();
  rerender({ enabled: true });
  await act(async () => vi.advanceTimersByTimeAsync(1));
  expect(fetch).toHaveBeenCalledTimes(1);
  const comment = { id: "reply", author: "bob", body: "Posted", created_at: null, url: null };
  act(() =>
    client.setQueryData<ReviewThreadsPage>(key, (page) =>
      appendReviewReply(page!, "thread", comment),
    ),
  );
  await act(async () => vi.advanceTimersByTimeAsync(10_010));
  expect(result.current.page?.data[0].comments.data.map(({ id }) => id)).toEqual(["root", "reply"]);
  serverPage = appendReviewReply(first, "thread", comment);
  await act(async () => vi.advanceTimersByTimeAsync(10_010));
  expect(result.current.page?.data[0].pending_comment_ids).toEqual([]);
  serverPage = first;
  await act(async () => vi.advanceTimersByTimeAsync(10_010));
  expect(result.current.page?.data[0].comments.data.map(({ id }) => id)).toEqual(["root"]);
  rerender({ enabled: false });
  const count = fetch.mock.calls.length;
  await act(async () => vi.advanceTimersByTimeAsync(60_000));
  expect(fetch).toHaveBeenCalledTimes(count);
  rerender({ enabled: true });
  await act(async () => vi.advanceTimersByTimeAsync(1));
  expect(fetch).toHaveBeenCalledTimes(count + 1);
  client.clear();
});

it("retains the local destination and settles a save after navigating away", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  let respond!: (response: Response) => void;
  const fetch = vi.fn(
    () =>
      new Promise<Response>((resolve) => {
        respond = resolve;
      }),
  );
  vi.stubGlobal("fetch", fetch);
  const wrapper = ({ children }: PropsWithChildren) => (
    <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, comment_anchor_metadata: true }}>
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    </CapabilitiesProvider>
  );
  const { result, rerender } = renderHook(
    ({ url }) => usePullRequestReviewThreadComposer("session", url, "app.py", "head", "base"),
    { wrapper, initialProps: { url: "pr" } },
  );
  act(() => result.current.setDestination("local"));
  act(() => result.current.selectLines({ side: "LEFT", line: 9, start_line: 7 }));
  act(() => result.current.setBody("Keep this check"));
  rerender({ url: "other" });
  rerender({ url: "pr" });
  expect(result.current.draft.destination).toBe("local");
  act(() => result.current.post());
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  expect(fetch).toHaveBeenCalledWith(
    "/v1/sessions/session/comments",
    expect.objectContaining({ method: "POST" }),
  );
  rerender({ url: "other" });
  act(() => result.current.setBody("Other PR draft"));
  await waitFor(() => expect(result.current.draft.body).toBe("Other PR draft"));
  await act(async () => respond(Response.json(savedLocalComment)));
  expect(result.current.draft.body).toBe("Other PR draft");
  rerender({ url: "pr" });
  await waitFor(() => expect(result.current.draft.pending).toBe(false));
  expect(result.current.draft.body).toBe("");
  expect(result.current.draft.destination).toBe("local");
  const contexts = renderHook(() => usePendingLocalReviewContexts("session"));
  expect(contexts.result.current).toEqual([savedLocalComment]);
});

const savedLocalComment: Comment = {
  id: "saved",
  conversation_id: "session",
  path: "app.py",
  body: "Keep this check",
  start_index: 0,
  end_index: 0,
  status: "draft",
  created_at: 0,
  updated_at: 0,
  created_by: null,
  anchor_content: null,
  anchor_metadata: {
    pr_url: "pr",
    side: "LEFT",
    line: 9,
    start_line: 7,
    head_sha: "head",
    base_sha: "base",
  },
};

it.each(["unsupported", "create failure", "legacy", "changed anchor", "cleanup failure"])(
  "preserves local feedback and coordinates after %s",
  async (failure) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (failure === "create failure") throw new TypeError("Network error");
      if (init?.method === "DELETE")
        return new Response(null, { status: failure === "cleanup failure" ? 500 : 204 });
      return Response.json({
        ...savedLocalComment,
        anchor_metadata:
          failure === "changed anchor" ? { ...savedLocalComment.anchor_metadata, line: 10 } : null,
      });
    });
    vi.stubGlobal("fetch", fetch);
    const wrapper = ({ children }: PropsWithChildren) => (
      <CapabilitiesProvider
        info={{ ...FALLBACK_SERVER_INFO, comment_anchor_metadata: failure !== "unsupported" }}
      >
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      </CapabilitiesProvider>
    );
    const { result } = renderHook(
      () => usePullRequestReviewThreadComposer("session", "pr", "app.py", "head", "base"),
      { wrapper },
    );
    act(() => result.current.setDestination("local"));
    act(() => result.current.selectLines({ side: "LEFT", line: 9, start_line: 7 }));
    act(() => result.current.setBody(savedLocalComment.body));
    act(() => result.current.post());
    await waitFor(() => expect(result.current.draft.error).toBeTruthy());
    expect(result.current.draft).toMatchObject({
      body: savedLocalComment.body,
      pending: false,
      destination: "local",
      anchor: {
        path: "app.py",
        side: "LEFT",
        line: 9,
        start_line: 7,
        head_sha: "head",
        base_sha: "base",
      },
    });
    const contexts = renderHook(() => usePendingLocalReviewContexts("session"));
    expect(contexts.result.current).toEqual([]);
    if (failure === "unsupported") {
      expect(fetch).not.toHaveBeenCalled();
      expect(result.current.draft.error).toContain("require a newer server");
    } else if (failure === "create failure") {
      expect(fetch).toHaveBeenCalledTimes(1);
      expect(result.current.draft.error).toContain("Network error");
    } else {
      expect(fetch).toHaveBeenCalledTimes(2);
      expect(fetch).toHaveBeenLastCalledWith(
        "/v1/sessions/session/comments/saved",
        expect.objectContaining({ method: "DELETE" }),
      );
      expect(result.current.draft.error).toContain(
        failure === "cleanup failure" ? "may remain in Files" : "Update the server before retrying",
      );
    }
  },
);
