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
import type { Comment } from "../../hooks/useComments";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

afterEach(() => {
  clearLocalReviewContexts("session");
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

it("remembers the last destination for new comments while preserving open drafts", async () => {
  const client = new QueryClient();
  const wrapper = ({ children }: PropsWithChildren) => (
    <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, comment_anchor_metadata: true }}>
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    </CapabilitiesProvider>
  );
  const initialProps = { session: "session", url: "pr", path: "app.py" };
  const useComposer = ({ session, url, path }: typeof initialProps) =>
    usePullRequestReviewThreadComposer(session, url, path, "head", "base");
  const { result, rerender, unmount } = renderHook(useComposer, { wrapper, initialProps });
  const lines = { side: "RIGHT" as const, line: 2, start_line: null };
  act(() => result.current.selectLines(lines));
  await waitFor(() => expect(result.current.draft.destination).toBe("github"));
  act(() => result.current.setDestination("local"));
  act(() => result.current.setBody("Keep this draft local"));
  rerender({ session: "another-session", url: "another-pr", path: "other.py" });
  act(() => result.current.selectLines(lines));
  await waitFor(() => expect(result.current.draft.destination).toBe("local"));
  act(() => result.current.setDestination("github"));
  rerender(initialProps);
  act(() => result.current.selectLines({ ...lines, line: 3 }));
  await waitFor(() =>
    expect(result.current.draft).toMatchObject({
      destination: "local",
      body: "Keep this draft local",
    }),
  );
  act(() => result.current.cancel());
  act(() => result.current.selectLines(lines));
  await waitFor(() => expect(result.current.draft.destination).toBe("github"));

  // A provider without GitHub posting uses Agent without changing the saved preference.
  act(() => result.current.cancel());
  act(() => result.current.selectLines(lines, "local"));
  await waitFor(() => expect(result.current.draft.destination).toBe("local"));
  act(() => result.current.cancel());
  act(() => result.current.selectLines(lines));
  await waitFor(() => expect(result.current.draft.destination).toBe("github"));

  act(() => result.current.setDestination("local"));
  unmount();
  client.clear();
  const remounted = renderHook(useComposer, { wrapper, initialProps });
  act(() => remounted.result.current.selectLines(lines));
  await waitFor(() => expect(remounted.result.current.draft.destination).toBe("local"));
});

it("retains a new range draft, requires reselection after a revision change, and isolates a late write", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(
    ["pr-page-github-review-threads", "session", "pr", "account", "base:head"],
    first,
  );
  client.setQueryData(
    ["pr-page-github-review-threads", "session", "pr", "account", "base:changed"],
    {
      ...first,
      head_sha: "changed",
    },
  );
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
  await act(async () => client.invalidateQueries({ queryKey: ["pr-page-github-review-threads"] }));
  await waitFor(() => expect(result.current.historyStale).toBe(true));
  expect(result.current.page?.data[0].comments.next_cursor).toBeNull();
  expect(result.current.page?.data[0].comments.has_more).toBe(false);
  fetch.mockImplementation(
    async () =>
      new Response(
        JSON.stringify({ ...first, data: [], warning: "Could not load conversations." }),
      ),
  );
  await act(async () => client.invalidateQueries({ queryKey: ["pr-page-github-review-threads"] }));
  await waitFor(() => expect(result.current.page?.warning).toBe("Could not load conversations."));
  expect(result.current.page?.data[0].comments.data).toHaveLength(2);
  fetch.mockImplementation(async () => new Response(JSON.stringify(first)));
  act(() => result.current.refreshHistory());
  await waitFor(() => expect(result.current.page?.data[0].comments.data).toHaveLength(1));
  expect(result.current.historyStale).toBe(false);
});

it("keeps late pagination results with the original PR and isolates revisions and accounts", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(
    ["pr-page-github-review-threads", "session", "pr", "alice", "base:head"],
    first,
  );
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
  const key = ["pr-page-github-review-threads", "session", "pr", "account", "base:head"];
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

it("keeps the cached thread objects through a poll that reads them unchanged", async () => {
  vi.useFakeTimers();
  const client = new QueryClient();
  const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify(first)));
  vi.stubGlobal("fetch", fetch);
  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { result } = renderHook(
    () => usePullRequestReviewThreads("session", "pr", "account", "base:head", true),
    { wrapper },
  );
  await act(async () => vi.advanceTimersByTimeAsync(1));
  const read = result.current.page?.data[0];
  expect(read).toBeDefined();
  // Two polls, the first of which used to rebuild every thread with keys of its own.
  await act(async () => vi.advanceTimersByTimeAsync(10_010));
  await act(async () => vi.advanceTimersByTimeAsync(10_010));
  expect(fetch).toHaveBeenCalledTimes(3);
  expect(result.current.page?.data[0]).toBe(read);
  client.clear();
});

it("stops polling a review read that failed, until it is asked again", async () => {
  vi.useFakeTimers();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const fetch = vi.fn(async () => new Response(JSON.stringify(first)));
  vi.stubGlobal("fetch", fetch);
  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { result } = renderHook(
    () => usePullRequestReviewThreads("session", "pr", "account", "base:head", true),
    { wrapper },
  );
  await act(() => vi.advanceTimersByTimeAsync(0));
  expect(result.current.page?.data).toHaveLength(1);

  // The next poll fails.
  let respond!: (response: Response) => void;
  fetch.mockImplementationOnce(
    () =>
      new Promise<Response>((resolve) => {
        respond = resolve;
      }),
  );
  await act(() => vi.advanceTimersByTimeAsync(10_010));
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(result.current.isFetching).toBe(true);
  await act(async () => respond(new Response("{}", { status: 502 })));
  await act(() => vi.advanceTimersByTimeAsync(0));
  expect(result.current.error).toBeTruthy();

  // After a failure it waits for Refresh rather than asking again.
  const calls = fetch.mock.calls.length;
  await act(() => vi.advanceTimersByTimeAsync(60_000));
  expect(fetch.mock.calls.length).toBe(calls);
  expect(result.current.isFetching).toBe(false);
});

it("keeps the loaded threads when a history refresh fails, and replaces them once one succeeds", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  let fail = false;
  const fetch = vi.fn().mockImplementation(async (url: string) => {
    if (fail)
      return new Response(JSON.stringify({ detail: "GitHub is unavailable" }), { status: 502 });
    return new Response(
      JSON.stringify(
        url.includes("/comments?")
          ? {
              data: [{ id: "reply", author: "bob", body: "Because", created_at: null, url: null }],
              has_more: false,
              next_cursor: null,
            }
          : first,
      ),
    );
  });
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

  fail = true;
  act(() => result.current.refreshHistory());
  await waitFor(() => expect(result.current.error).toBe("GitHub is unavailable"));
  // The loaded history stays on screen while the refresh fails.
  expect(result.current.page?.data[0].comments.data).toHaveLength(2);
  act(() => result.current.retry());
  await waitFor(() => expect(fetch.mock.calls.length).toBeGreaterThan(3));
  expect(result.current.page?.data[0].comments.data).toHaveLength(2);

  fail = false;
  act(() => result.current.refreshHistory());
  await waitFor(() => expect(result.current.page?.data[0].comments.data).toHaveLength(1));
  expect(result.current.error).toBeUndefined();
  expect(result.current.historyStale).toBe(false);
});

it.each([
  ["the next page of threads", "threads" as const, "after=threads-2"],
  ["a thread's next replies", "replies" as const, "/comments?"],
])(
  "retries %s from where it stopped, keeping loaded history and clearing the failure",
  async (_label, failure, failedRead) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let failPaging = true;
    const fetch = vi.fn().mockImplementation(async (url: string) => {
      const paging = url.includes("/comments?") || url.includes("after=threads-2");
      if (paging && failPaging) {
        return new Response(JSON.stringify({ detail: "GitHub is unavailable" }), { status: 502 });
      }
      if (url.includes("/comments?")) {
        return new Response(
          JSON.stringify({
            data: [{ id: "reply", author: "bob", body: "Because", created_at: null, url: null }],
            has_more: false,
            next_cursor: null,
          }),
        );
      }
      if (url.includes("after=threads-2")) {
        return new Response(
          JSON.stringify({
            ...first,
            has_more: false,
            next_cursor: null,
            data: [{ ...first.data[0], id: "second-thread" }],
          }),
        );
      }
      return new Response(JSON.stringify(first));
    });
    vi.stubGlobal("fetch", fetch);
    const wrapper = ({ children }: PropsWithChildren) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(
      () => usePullRequestReviewThreads("session", "pr", "account", "base:head", true),
      { wrapper },
    );
    await waitFor(() => expect(result.current.page?.data).toHaveLength(1));
    act(() =>
      failure === "threads"
        ? result.current.loadMore()
        : result.current.loadComments(result.current.page!.data[0]),
    );
    await waitFor(() => expect(result.current.failure).toBe(failure));
    expect(result.current.error).toBe("GitHub is unavailable");
    expect(result.current.page?.data).toHaveLength(1);

    failPaging = false;
    const before = fetch.mock.calls.length;
    act(() => result.current.retry());
    await waitFor(() => expect(result.current.failure).toBeUndefined());
    // Try again asks for what failed, not only the first page.
    expect(fetch.mock.calls.slice(before).some(([url]) => String(url).includes(failedRead))).toBe(
      true,
    );
    expect(result.current.error).toBeUndefined();
    if (failure === "threads") {
      expect(result.current.page?.data.map((thread) => thread.id)).toEqual([
        "thread",
        "second-thread",
      ]);
    } else {
      expect(result.current.page?.data[0].comments.data.map((comment) => comment.id)).toEqual([
        "root",
        "reply",
      ]);
    }
  },
);

it("keeps a failed refresh's replacement to its own account, so another account's history stays", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const pagedThread = { ...first.data[0], id: "paged-thread" };
  // Bob's history includes a thread from a later page, beyond the first page's.
  client.setQueryData(["pr-page-github-review-threads", "session", "pr", "bob", "base:head"], {
    ...first,
    data: [...first.data, pagedThread],
    first_page_ids: ["thread"],
  });
  let failAlice = true;
  const fetch = vi
    .fn()
    .mockImplementation(async () =>
      failAlice
        ? new Response(JSON.stringify({ detail: "GitHub is unavailable" }), { status: 502 })
        : new Response(JSON.stringify(first)),
    );
  vi.stubGlobal("fetch", fetch);
  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { result, rerender } = renderHook(
    ({ account }) => usePullRequestReviewThreads("session", "pr", account, "base:head", true),
    { wrapper, initialProps: { account: "alice" } },
  );
  await waitFor(() => expect(result.current.error).toBeDefined());
  // Alice's refresh fails, so her replacement is still waiting when the account switches.
  act(() => result.current.refreshHistory());
  await waitFor(() => expect(fetch.mock.calls.length).toBeGreaterThan(1));
  await waitFor(() => expect(result.current.error).toBe("GitHub is unavailable"));

  failAlice = false;
  const before = fetch.mock.calls.length;
  rerender({ account: "bob" });
  // Bob's own read has to answer before his history can be judged.
  await waitFor(() => expect(fetch.mock.calls.length).toBeGreaterThan(before));
  await waitFor(() => expect(result.current.isFetching).toBe(false));
  expect(result.current.error).toBeUndefined();
  expect(result.current.page?.data.map((thread) => thread.id)).toEqual(["thread", "paged-thread"]);
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
  act(() => result.current.selectLines({ side: "RIGHT", line: 2, start_line: null }));
  await waitFor(() => expect(result.current.draft.destination).toBe("local"));
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

it("keeps a PR's threads for 30 minutes after its Diff view closes, as its overview is kept", async () => {
  vi.useFakeTimers();
  const client = new QueryClient();
  const key = ["pr-page-github-review-threads", "session", "pr", "account", "base:head"];
  const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify(first)));
  vi.stubGlobal("fetch", fetch);
  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const view = () =>
    renderHook(() => usePullRequestReviewThreads("session", "pr", "account", "base:head", true), {
      wrapper,
    });
  const opened = view();
  await act(async () => vi.advanceTimersByTimeAsync(1));
  expect(opened.result.current.page?.data[0].id).toBe("thread");
  opened.unmount();
  // Past the 5 minutes an unused query is otherwise kept: back on the PR, a slow read shows them.
  await act(async () => vi.advanceTimersByTimeAsync(5.5 * 60_000));
  fetch.mockImplementation(() => new Promise<Response>(() => {}));
  const returned = view();
  expect(returned.result.current.page?.data[0].id).toBe("thread");
  returned.unmount();
  await act(async () => vi.advanceTimersByTimeAsync(30 * 60_000 + 1_000));
  expect(client.getQueryData(key)).toBeUndefined();
  client.clear();
});
