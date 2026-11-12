import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import {
  childSessionsQueryKey,
  fetchChildSessions,
  useChildSessions,
  useDescendantSession,
} from "./useChildSessions";

vi.mock("@/lib/identity", () => ({
  authenticatedFetch: vi.fn(),
}));

function wrapper({ children }: PropsWithChildren) {
  return (
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      {children}
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.mocked(authenticatedFetch).mockReset();
});

describe("useChildSessions", () => {
  it("does not fetch child sessions for a provisional conversation", () => {
    const { result } = renderHook(() => useChildSessions("temp:pending-create"), { wrapper });

    expect(result.current.children).toEqual([]);
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });
});

describe("useDescendantSession", () => {
  it("fetches the direct-child list when it is not cached", async () => {
    // WHY: a mirrored card can mount before anything loaded the ancestor's
    // child list; the lookup must fetch it rather than stay unlabeled.
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          object: "list",
          data: [
            {
              id: "conv_child",
              title: "researcher:auth",
              task_summary: "Investigate auth flow",
              tool: "researcher",
              session_name: "auth",
              current_task_status: "in_progress",
              busy: true,
            },
          ],
        }),
      ),
    );
    const { result } = renderHook(() => useDescendantSession("conv_parent", "conv_child"), {
      wrapper,
    });

    expect(result.current).toBeNull();
    await waitFor(() => expect(result.current?.task_summary).toBe("Investigate auth flow"));
    expect(authenticatedFetch).toHaveBeenCalledWith(
      "/v1/sessions/conv_parent/child_sessions?limit=100",
      { signal: expect.any(AbortSignal) },
    );
  });
});

it("loads every bounded page and rejects a repeated cursor", async () => {
  const fetch = vi.mocked(authenticatedFetch);
  const page = (id: string, more: boolean) =>
    new Response(JSON.stringify({ data: [{ id, own_cost_usd: 0 }], has_more: more, last_id: id }));
  fetch.mockResolvedValueOnce(page("first", true)).mockResolvedValueOnce(page("last", false));
  const signal = new AbortController().signal;
  expect(
    (await fetchChildSessions("parent", signal)).map((child) => [child.id, child.own_cost_usd]),
  ).toEqual([
    ["first", 0],
    ["last", 0],
  ]);
  expect(fetch).toHaveBeenLastCalledWith(
    "/v1/sessions/parent/child_sessions?limit=100&after=first",
    { signal },
  );
  fetch.mockResolvedValueOnce(page("first", true)).mockResolvedValueOnce(page("first", true));
  await expect(fetchChildSessions("parent")).rejects.toThrow("Repeated child-session cursor");
});

it("fetches the complete list when SSE seeded only a partial cold cache", async () => {
  const client = new QueryClient();
  client.setQueryData(childSessionsQueryKey("parent"), [{ id: "partial" }], { updatedAt: 0 });
  vi.mocked(authenticatedFetch).mockResolvedValueOnce(
    new Response(JSON.stringify({ data: [{ id: "first" }, { id: "second" }] })),
  );
  const { result } = renderHook(() => useChildSessions("parent"), {
    wrapper: ({ children }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
  await waitFor(() =>
    expect(result.current.children.map((child) => child.id)).toEqual(["first", "second"]),
  );
});
