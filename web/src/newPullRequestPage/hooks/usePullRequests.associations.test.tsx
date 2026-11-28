import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import type * as WorkspaceChangedFiles from "../../hooks/useWorkspaceChangedFiles";
import {
  usePullRequestChangedFiles,
  usePullRequestDiff,
  usePullRequestInfo,
  useUpdateSessionPr,
} from "./usePullRequests";
import { changedLineTotals } from "@/newPullRequestPage/shell/PullRequestPanel";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

vi.mock("../../hooks/useWorkspaceChangedFiles", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkspaceChangedFiles>()),
  useWorkspaceServeable: () => true,
}));

afterEach(() => vi.unstubAllGlobals());

it.each([
  { url: "https://github.com/team/project/pull/7", staleSelection: false },
  { url: "https://github.com/team/project/pull/7", staleSelection: true },
  { url: "https://gitlab.com/team/project/-/merge_requests/7", staleSelection: false },
  { url: "https://gitlab.com/team/project/-/merge_requests/7", staleSelection: true },
])(
  "does not refetch an unlinked URL while the panel switches selection: $url, stale=$staleSelection",
  async ({ url, staleSelection }) => {
    const empty = {
      object: "session.github.info",
      available: true,
      tracking_available: true,
      prs: [],
      pr: null,
    };
    const linked = {
      ...empty,
      selected_pr_url: url,
      prs: [
        {
          url,
          host: new URL(url).host,
          repository: "team/project",
          number: 7,
          relationship: "attached",
        },
      ],
    };
    let removed = false;
    const staleRequests: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = new URL(String(input), "http://localhost");
        if (init?.method === "POST") {
          removed = JSON.parse(String(init.body)).action === "remove";
        } else if (removed && request.searchParams.get("pr_url") === url) {
          staleRequests.push(request.href);
          return new Response("No longer tracked", { status: 502 });
        }
        const unlinked = { ...empty, ...(staleSelection ? { selected_pr_url: url } : {}) };
        return new Response(JSON.stringify(removed ? unlinked : linked), { status: 200 });
      }),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(["pr-page-github-info", "conv"], linked);
    client.setQueryData(["pr-page-github-info", "conv", url], linked);
    const { result, rerender, unmount } = renderHook(
      ({ prUrl }: { prUrl?: string }) => ({
        info: usePullRequestInfo("conv", { prUrl }),
        update: useUpdateSessionPr("conv"),
      }),
      {
        initialProps: { prUrl: url as string | undefined },
        wrapper: ({ children }) => (
          <QueryClientProvider client={client}>{children}</QueryClientProvider>
        ),
      },
    );

    await act(() => result.current.update.mutateAsync({ url, action: "remove" }));
    // React can render the observer before the panel commits its new selection.
    rerender({ prUrl: url });
    await waitFor(() => expect(result.current.info.isFetching).toBe(false));
    expect(staleRequests).toEqual([]);

    rerender({ prUrl: undefined });
    await waitFor(() => expect(result.current.info.data).toMatchObject(empty));
    await act(() => result.current.update.mutateAsync({ url, action: "attach" }));
    rerender({ prUrl: url });
    await waitFor(() => expect(result.current.info.data?.selected_pr_url).toBe(url));
    expect(staleRequests).toEqual([]);
    unmount();
    client.clear();
  },
);

it("preserves changed-file incompleteness from the host response", async () => {
  const payload = {
    object: "list",
    data: [],
    has_more: true,
    warning: "The provider could not load every changed file.",
  };
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), { status: 200 })),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { result } = renderHook(() => usePullRequestChangedFiles("conv", true), {
    wrapper: ({ children }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });

  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.data).toEqual({ ...payload, available: true });
});

it("reads a file's line counts as unknown when the provider leaves them out", async () => {
  const counted = {
    path: "a.ts",
    name: "a.ts",
    status: "modified",
    lines_added: 2,
    lines_removed: 1,
  };
  const uncounted = { path: "b.ts", name: "b.ts", status: "modified" };
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ object: "list", data: [counted, uncounted] }), {
        status: 200,
      }),
    ),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { result } = renderHook(() => usePullRequestChangedFiles("conv", true), {
    wrapper: ({ children }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });

  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.data?.data).toEqual([
    counted,
    { ...uncounted, lines_added: null, lines_removed: null },
  ]);
  // Unknown counts leave the totals out rather than adding up to NaN.
  expect(changedLineTotals(result.current.data!.data)).toBeUndefined();
});

it("pins the changed-file list to the same revision as the diff", async () => {
  const url = "https://github.com/example/one/pull/42";
  const revision = `${"b".repeat(40)}:${"a".repeat(40)}`;
  const fetchMock = vi.fn(
    async () => new Response(JSON.stringify({ object: "list", data: [] }), { status: 200 }),
  );
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { result } = renderHook(
    () => ({
      changes: usePullRequestChangedFiles("conv", true, url, revision),
      diff: usePullRequestDiff("conv", true, url, revision),
    }),
    {
      wrapper: ({ children }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      ),
    },
  );

  await waitFor(() =>
    expect(result.current.changes.isSuccess && result.current.diff.isSuccess).toBe(true),
  );
  const queries = fetchMock.mock.calls.map((call: unknown[]) => {
    const request = new URL(String(call[0]), "http://localhost");
    return [request.pathname.split("/").pop(), Object.fromEntries(request.searchParams)];
  });
  const pinned = { pr_url: url, base_sha: "b".repeat(40), head_sha: "a".repeat(40) };
  expect(queries).toEqual(
    expect.arrayContaining([
      ["changes", pinned],
      ["diff", pinned],
    ]),
  );
});

it("reads the session's info again instead of installing the reply", async () => {
  const url = "https://github.com/example/one/pull/42";
  const empty = { object: "session.github.info", tracking_available: true, prs: [] };
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response(JSON.stringify(empty), { status: 200 })),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const before = { selected_pr_url: url, prs: [{ url }] };
  client.setQueryData(["pr-page-github-info", "conv"], before);
  const { result } = renderHook(() => useUpdateSessionPr("conv"), {
    wrapper: ({ children }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
  await act(() => result.current.mutateAsync({ url, action: "attach" }));
  // A reply that arrives after a newer change would undo it; only the read decides.
  expect(client.getQueryData(["pr-page-github-info", "conv"])).toBe(before);
  expect(client.getQueryState(["pr-page-github-info", "conv"])?.isInvalidated).toBe(true);
});

it("keeps the workspace's repo when an attach reply describes only the PR", async () => {
  const url = "https://github.com/example/other/pull/7";
  const workspaceRepo = { name_with_owner: "example/one", host: "github.com" };
  const attached = { object: "session.github.info", selected_pr_url: url, prs: [{ url }] };
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response(JSON.stringify(attached), { status: 200 })),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(["pr-page-github-info", "conv"], {
    object: "session.github.info",
    prs: [],
    workspace_repo: workspaceRepo,
  });
  const { result } = renderHook(() => useUpdateSessionPr("conv"), {
    wrapper: ({ children }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
  await act(() => result.current.mutateAsync({ url, action: "attach" }));
  // The reply writes nothing; the list is read again, and until then keeps its repo.
  expect(client.getQueryData(["pr-page-github-info", "conv"])).toMatchObject({
    workspace_repo: workspaceRepo,
  });
  expect(client.getQueryState(["pr-page-github-info", "conv"])?.isInvalidated).toBe(true);
});

it("keeps a link to its own session when the panel switches sessions", async () => {
  const url = "https://github.com/example/one/pull/42";
  const linked = { object: "session.github.info", tracking_available: true, prs: [{ url }] };
  const other = { object: "session.github.info", tracking_available: true, prs: [] };
  let answer: (response: Response) => void = () => {};
  vi.stubGlobal(
    "fetch",
    vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          answer = resolve;
        }),
    ),
  );
  const client = new QueryClient();
  client.setQueryData(["pr-page-github-info", "conv_1"], other);
  client.setQueryData(["pr-page-github-info", "conv_2"], other);
  const { result, rerender } = renderHook(({ id }) => useUpdateSessionPr(id), {
    initialProps: { id: "conv_1" },
    wrapper: ({ children }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
  result.current.mutate({ url, action: "attach" });
  await waitFor(() => expect(result.current.isPending).toBe(true));

  rerender({ id: "conv_2" });
  expect(result.current.isIdle).toBe(true);
  answer(new Response(JSON.stringify(linked), { status: 200 }));

  // The link's own session is read again; the session the panel switched to is left alone.
  await waitFor(() =>
    expect(client.getQueryState(["pr-page-github-info", "conv_1"])?.isInvalidated).toBe(true),
  );
  expect(client.getQueryState(["pr-page-github-info", "conv_2"])?.isInvalidated).toBe(false);
  expect(client.getQueryData(["pr-page-github-info", "conv_2"])).toEqual(other);
});

it("does not restore an unlinked GitLab MR from an older host's stale selection", async () => {
  const url = "https://gitlab.com/team/project/-/merge_requests/7";
  const empty = {
    object: "session.github.info",
    available: true,
    provider: "gitlab",
    tracking_available: true,
    prs: [],
    pr: null,
    selected_pr_url: url,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response(JSON.stringify(empty), { status: 200 })),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { result } = renderHook(() => useUpdateSessionPr("conv"), {
    wrapper: ({ children }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
  await act(async () => {
    const response = await result.current.mutateAsync({ url, action: "remove" });
    expect(response.selected_pr_url).toBeUndefined();
  });
});
