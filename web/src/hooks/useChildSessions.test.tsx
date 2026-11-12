import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import { fetchChildSessions, useChildSessions, useDescendantSession } from "./useChildSessions";

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
  it("loads later cursor pages into the shared child list", async () => {
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            data: Array.from({ length: 20 }, (_, index) => ({ id: `child-${index + 1}` })),
            has_more: true,
            last_id: "child-20",
          }),
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            data: [{ id: "child-21", own_cost_usd: 1.25 }],
            has_more: false,
            last_id: "child-21",
          }),
        ),
      );
    const { result } = renderHook(() => useChildSessions("parent"), { wrapper });
    await waitFor(() => expect(result.current.children).toHaveLength(21));
    expect(result.current.children.at(-1)).toMatchObject({ id: "child-21", own_cost_usd: 1.25 });
    expect(authenticatedFetch).toHaveBeenNthCalledWith(
      2,
      "/v1/sessions/parent/child_sessions?limit=100&after=child-20",
      { signal: expect.any(AbortSignal) },
    );
  });

  it("rejects a repeated pagination cursor instead of looping or claiming a complete list", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(
      async () =>
        new Response(
          JSON.stringify({
            data: [{ id: "child" }],
            has_more: true,
            last_id: "child",
          }),
        ),
    );
    await expect(fetchChildSessions("parent")).rejects.toThrow("pagination did not advance");
    expect(authenticatedFetch).toHaveBeenCalledTimes(2);
  });

  it("preserves zero, priced and missing own-agent costs", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          data: [
            { id: "free", own_cost_usd: 0 },
            { id: "priced", own_cost_usd: 1.25 },
            { id: "older" },
          ],
        }),
      ),
    );
    const children = await fetchChildSessions("parent");
    expect(children.map((child) => child.own_cost_usd)).toEqual([0, 1.25, null]);
  });
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
