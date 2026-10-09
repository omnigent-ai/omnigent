import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import type * as WorkspaceChangedFiles from "./useWorkspaceChangedFiles";
import {
  usePullRequestChangedFiles,
  usePullRequestInfo,
  useUpdateSessionPr,
} from "./usePullRequests";

vi.mock("./useWorkspaceChangedFiles", async (importOriginal) => ({
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
    client.setQueryData(["github-info", "conv"], linked);
    client.setQueryData(["github-info", "conv", url], linked);
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
    expect(result.current.info.data).toMatchObject(empty);
    expect(result.current.info.data?.selected_pr_url).toBeUndefined();

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

it("replaces the default selection and the removed PR's cached metadata", async () => {
  const url = "https://github.com/example/one/pull/42";
  const empty = { object: "session.github.info", tracking_available: true, prs: [] };
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response(JSON.stringify(empty), { status: 200 })),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(["github-info", "conv"], { selected_pr_url: url });
  client.setQueryData(["github-info", "conv", url], { selected_pr_url: url });
  const { result } = renderHook(() => useUpdateSessionPr("conv"), {
    wrapper: ({ children }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
  await act(() => result.current.mutateAsync({ url, action: "remove" }));
  expect(client.getQueryData(["github-info", "conv"])).toMatchObject(empty);
  expect(client.getQueryData(["github-info", "conv", url])).toMatchObject(empty);
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
  expect(client.getQueryData(["github-info", "conv"])).toMatchObject({
    prs: [],
    selected_pr_url: undefined,
  });
  expect(client.getQueryData(["github-info", "conv", url])).toMatchObject({
    prs: [],
    selected_pr_url: undefined,
  });
});
