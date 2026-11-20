import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO, type FeatureValues } from "@/lib/capabilities";

import { useDeleteWorkflow, useSavedWorkflows, useSharedWorkflows } from "./useSavedWorkflows";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: vi.fn() }));

beforeEach(() => {
  vi.mocked(authenticatedFetch).mockReset();
  vi.mocked(useServerInfo).mockReturnValue({
    ...FALLBACK_SERVER_INFO,
    workflows: true,
    features: { workflows: true },
  });
});

function renderWithClient<T>(hook: () => T, client = new QueryClient()) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, ...renderHook(hook, { wrapper }) };
}

describe("useSavedWorkflows", () => {
  afterEach(cleanup);

  const disabledFeatures: FeatureValues[] = [{}, { workflows: false }];
  it.each(disabledFeatures)(
    "hides cached workflows and makes no request when the flag is absent or off: %j",
    (features) => {
      vi.mocked(useServerInfo).mockReturnValue({
        ...FALLBACK_SERVER_INFO,
        workflows: true,
        features,
      });
      const client = new QueryClient();
      client.setQueryData(["saved-workflows"], {
        pages: [{ data: [{ id: "saved", name: "Release", description: "" }], has_more: false }],
        pageParams: [null],
      });
      const { result } = renderWithClient(() => useSavedWorkflows(), client);

      act(() => result.current.refresh());

      expect(result.current.workflows).toEqual([]);
      expect(authenticatedFetch).not.toHaveBeenCalled();
    },
  );

  it("reports a failed load", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(
      new Response(null, { status: 500, statusText: "Server Error" }),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderWithClient(() => useSavedWorkflows(), client);

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.isLoading).toBe(false);
    expect(result.current.workflows).toEqual([]);
  });

  it.each([undefined, "name:V29ya2Zsb3cgMTk5"])(
    "loads another bounded page only on request with cursor %s",
    async (cursor) => {
      const first = Array.from({ length: 200 }, (_, index) => ({
        id: `workflow-${index}`,
        name: `Workflow ${index}`,
        description: "",
      }));
      const last = { id: "workflow-200", name: "Last workflow", description: "" };
      vi.mocked(authenticatedFetch)
        .mockResolvedValueOnce(
          new Response(
            JSON.stringify({
              data: first,
              has_more: true,
              last_id: "workflow-199",
              next_cursor: cursor,
            }),
          ),
        )
        .mockResolvedValueOnce(
          new Response(
            JSON.stringify({
              data: [last],
              has_more: false,
              last_id: last.id,
            }),
          ),
        );
      const { result } = renderWithClient(() => useSavedWorkflows());
      await waitFor(() => expect(result.current.workflows).toHaveLength(200));
      expect(authenticatedFetch).toHaveBeenCalledTimes(1);
      expect(result.current.pagination.hasMore).toBe(true);
      act(() => result.current.pagination.loadMore());
      await waitFor(() => expect(result.current.workflows).toHaveLength(201));
      expect(result.current.workflows.at(-1)).toEqual(last);
      expect(result.current.pagination.hasMore).toBe(false);
      expect(authenticatedFetch).toHaveBeenLastCalledWith(
        `/v1/workflows?limit=200&after=${encodeURIComponent(cursor ?? "workflow-199")}`,
        expect.anything(),
      );
    },
  );
});

describe("workflows shared with the user", () => {
  afterEach(cleanup);

  const SHARING = {
    ...FALLBACK_SERVER_INFO,
    workflows: true,
    workflow_sharing: true,
    features: { workflows: true },
  };
  const own = { id: "own", name: "Deploy", description: "" };
  const shared = {
    id: "b".repeat(32),
    name: "Release",
    description: "",
    viewer_role: "viewer" as const,
    owner_display_name: "Alice",
  };
  const later = { ...shared, id: "c".repeat(32), name: "Rollback" };

  function respond(url: string): Response {
    if (url.startsWith("/v1/workflows/shared")) {
      return Response.json(
        url.includes("after=")
          ? { data: [later], next_cursor: null }
          : { data: [shared], next_cursor: shared.id },
      );
    }
    return Response.json({ data: [own], has_more: false, last_id: own.id });
  }

  it("pages the shared list by its cursor", async () => {
    vi.mocked(useServerInfo).mockReturnValue(SHARING);
    vi.mocked(authenticatedFetch).mockImplementation(async (url) => respond(String(url)));
    const { result } = renderWithClient(() => useSharedWorkflows());

    await waitFor(() => expect(result.current.workflows).toEqual([shared]));
    act(() => result.current.pagination.loadMore());
    await waitFor(() => expect(result.current.workflows).toEqual([shared, later]));
    expect(vi.mocked(authenticatedFetch).mock.calls.map(([url]) => url)).toEqual([
      "/v1/workflows/shared?limit=100",
      `/v1/workflows/shared?limit=100&after=${shared.id}`,
    ]);
  });

  it("lists them after the user's own only when asked, loading more of either on request", async () => {
    vi.mocked(useServerInfo).mockReturnValue(SHARING);
    vi.mocked(authenticatedFetch).mockImplementation(async (url) => respond(String(url)));
    const { result } = renderWithClient(() => useSavedWorkflows({ includeShared: true }));

    await waitFor(() => expect(result.current.workflows).toEqual([own, shared]));
    expect(result.current.pagination.hasMore).toBe(true);
    act(() => result.current.pagination.loadMore());
    await waitFor(() => expect(result.current.workflows).toEqual([own, shared, later]));

    cleanup();
    vi.mocked(authenticatedFetch).mockClear();
    const plain = renderWithClient(() => useSavedWorkflows());
    await waitFor(() => expect(plain.result.current.workflows).toEqual([own]));
    expect(vi.mocked(authenticatedFetch).mock.calls.map(([url]) => url)).toEqual([
      "/v1/workflows?limit=200",
    ]);
  });

  it.each([
    ["empty", Response.json({ data: [], next_cursor: null })],
    ["failed", new Response(null, { status: 500, statusText: "Server Error" })],
  ])(
    "refreshes both lists when the mounted combined list is refreshed (shared list %s)",
    async (_label, first) => {
      vi.mocked(useServerInfo).mockReturnValue(SHARING);
      let sharedCalls = 0;
      vi.mocked(authenticatedFetch).mockImplementation(async (url) => {
        if (!String(url).startsWith("/v1/workflows/shared")) return respond(String(url));
        sharedCalls += 1;
        return sharedCalls === 1
          ? first.clone()
          : Response.json({ data: [shared], next_cursor: null });
      });
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const { result } = renderWithClient(() => useSavedWorkflows({ includeShared: true }), client);
      await waitFor(() => expect(sharedCalls).toBe(1));
      await waitFor(() => expect(result.current.workflows).toEqual([own]));

      // Shared since the list loaded, or the service recovered: a refresh shows it.
      act(() => result.current.refresh());
      await waitFor(() => expect(result.current.workflows).toEqual([own, shared]));
      expect(sharedCalls).toBe(2);
    },
  );

  it.each([
    ["a server that doesn't share workflows", { ...SHARING, workflow_sharing: false }],
    ["a single-user server", { ...SHARING, single_user: true }],
  ])("asks nothing of %s", async (_label, info) => {
    vi.mocked(useServerInfo).mockReturnValue(info);
    vi.mocked(authenticatedFetch).mockImplementation(async (url) => respond(String(url)));
    const { result } = renderWithClient(() => useSavedWorkflows({ includeShared: true }));

    await waitFor(() => expect(result.current.workflows).toEqual([own]));
    expect(result.current.pagination.hasMore).toBe(false);
    expect(vi.mocked(authenticatedFetch).mock.calls.map(([url]) => url)).toEqual([
      "/v1/workflows?limit=200",
    ]);
  });
});

describe("useDeleteWorkflow", () => {
  afterEach(cleanup);

  it("deletes the workflow by ID, then reloads the list", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(new Response(null, { status: 204 }));
    const { client, result } = renderWithClient(() => useDeleteWorkflow());
    const invalidate = vi.spyOn(client, "invalidateQueries");

    await act(() => result.current.mutateAsync("0123abcd"));

    expect(authenticatedFetch).toHaveBeenCalledWith("/v1/workflows/0123abcd", {
      method: "DELETE",
    });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["saved-workflows"] });
  });

  it("fails on an error status", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(
      new Response(null, { status: 404, statusText: "Not Found" }),
    );
    const { result } = renderWithClient(() => useDeleteWorkflow());

    await act(async () => {
      await expect(result.current.mutateAsync("missing")).rejects.toThrow("404 Not Found");
    });
  });
});
