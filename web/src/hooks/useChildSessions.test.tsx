import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import { useChildSessions, useDescendantSession } from "./useChildSessions";

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

  it("requests the asked-for page size instead of the server default", async () => {
    // WHY: a caller that must see every child (the Clone dialog's running
    // sub-agent warning) cannot rely on the default first page of 20.
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          object: "list",
          data: [
            {
              id: "conv_old_worker",
              title: "worker:old",
              tool: "worker",
              session_name: "old",
              current_task_status: "in_progress",
              busy: true,
            },
          ],
        }),
      ),
    );
    const { result } = renderHook(() => useChildSessions("conv_parent", null, { limit: 1000 }), {
      wrapper,
    });

    await waitFor(() => expect(result.current.children).toHaveLength(1));
    expect(result.current.children[0]).toMatchObject({ id: "conv_old_worker", busy: true });
    expect(authenticatedFetch).toHaveBeenCalledWith(
      "/v1/sessions/conv_parent/child_sessions?limit=1000",
    );
  });

  it("follows continuation cursors when every child is requested", async () => {
    // WHY: the endpoint pages newest-first, so an old running worker behind
    // a full first page of idle children must still reach the caller.
    const page = (data: object[], has_more: boolean, last_id: string) =>
      new Response(JSON.stringify({ object: "list", data, has_more, last_id }));
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(
        page([{ id: "conv_new_idle", title: "worker:new", busy: false }], true, "conv_new_idle"),
      )
      .mockResolvedValueOnce(
        page([{ id: "conv_old_busy", title: "worker:old", busy: true }], false, "conv_old_busy"),
      );
    const { result } = renderHook(
      () => useChildSessions("conv_parent", null, { limit: 1, allPages: true }),
      { wrapper },
    );

    await waitFor(() => expect(result.current.children).toHaveLength(2));
    expect(result.current.children.map((child) => [child.id, child.busy])).toEqual([
      ["conv_new_idle", false],
      ["conv_old_busy", true],
    ]);
    expect(authenticatedFetch).toHaveBeenNthCalledWith(
      1,
      "/v1/sessions/conv_parent/child_sessions?limit=1",
    );
    expect(authenticatedFetch).toHaveBeenNthCalledWith(
      2,
      "/v1/sessions/conv_parent/child_sessions?limit=1&after=conv_new_idle",
    );
  });

  it("fails instead of paging forever when the cursor does not advance", async () => {
    const stuck = (data: object[]) =>
      new Response(JSON.stringify({ object: "list", data, has_more: true, last_id: "conv_stuck" }));
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(stuck([{ id: "conv_stuck", title: "worker:a", busy: false }]))
      .mockResolvedValueOnce(stuck([{ id: "conv_stuck", title: "worker:a", busy: false }]));
    const { result } = renderHook(
      () => useChildSessions("conv_parent", null, { limit: 1, allPages: true }),
      { wrapper },
    );

    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.error?.message).toBe("child_sessions cursor did not advance");
    expect(result.current.children).toEqual([]);
    expect(authenticatedFetch).toHaveBeenCalledTimes(2);
  });
  it("fails when a page claims more children without a cursor", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          object: "list",
          data: [{ id: "conv_a", title: "worker:a", busy: false }],
          has_more: true,
          last_id: null,
        }),
      ),
    );
    const { result } = renderHook(
      () => useChildSessions("conv_parent", null, { limit: 1, allPages: true }),
      { wrapper },
    );

    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.error?.message).toBe(
      "child_sessions reported more pages without a cursor",
    );
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
  });

  it("fails when the cursor cycles instead of advancing", async () => {
    const page = (last_id: string) =>
      new Response(
        JSON.stringify({
          object: "list",
          data: [{ id: last_id, title: "worker:x", busy: false }],
          has_more: true,
          last_id,
        }),
      );
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(page("conv_a"))
      .mockResolvedValueOnce(page("conv_b"))
      .mockResolvedValueOnce(page("conv_a"));
    const { result } = renderHook(
      () => useChildSessions("conv_parent", null, { limit: 1, allPages: true }),
      { wrapper },
    );

    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.error?.message).toBe("child_sessions cursor did not advance");
    expect(authenticatedFetch).toHaveBeenCalledTimes(3);
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
    expect(authenticatedFetch).toHaveBeenCalledWith("/v1/sessions/conv_parent/child_sessions");
  });
});
