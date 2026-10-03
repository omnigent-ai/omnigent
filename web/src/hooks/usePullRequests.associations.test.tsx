import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import type * as WorkspaceChangedFiles from "./useWorkspaceChangedFiles";
import { usePullRequestChangedFiles, useUpdateSessionPr } from "./usePullRequests";

vi.mock("./useWorkspaceChangedFiles", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkspaceChangedFiles>()),
  useWorkspaceServeable: () => true,
}));

afterEach(() => vi.unstubAllGlobals());

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

it("replaces the default selection and clears the removed PR cache", async () => {
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
  expect(client.getQueryData(["github-info", "conv"])).toEqual(empty);
  expect(client.getQueryData(["github-info", "conv", url])).toBeUndefined();
});
