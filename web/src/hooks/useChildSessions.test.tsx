import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import { useChildSessions, useDescendantSession, useDescendantSessions } from "./useChildSessions";

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
    expect(authenticatedFetch).toHaveBeenCalledWith("/v1/sessions/conv_parent/child_sessions");
  });
});

describe("useDescendantSessions", () => {
  it("fetches every level and flattens the tree in Agents-rail order", async () => {
    // WHY: the sub-agents grid pages through grandchildren too, so nested
    // levels must be discovered without the rail having expanded them.
    const tree: Record<string, string[]> = {
      conv_root: ["conv_a", "conv_b"],
      conv_a: ["conv_a1"],
      conv_a1: ["conv_a1x"],
      conv_b: [],
    };
    vi.mocked(authenticatedFetch).mockImplementation(async (url) => {
      const parent = String(url).split("/")[3]!;
      const data = (tree[parent] ?? []).map((id) => ({
        id,
        title: null,
        tool: null,
        session_name: null,
        current_task_status: null,
        busy: false,
      }));
      return new Response(JSON.stringify({ object: "list", data }));
    });
    const { result } = renderHook(() => useDescendantSessions("conv_root"), { wrapper });

    await waitFor(() =>
      expect(result.current.descendants.map((child) => child.id)).toEqual([
        "conv_a",
        "conv_a1",
        "conv_a1x",
        "conv_b",
      ]),
    );
    expect(result.current.isLoading).toBe(false);
    // The deepest level is listed but not expanded further.
    expect(authenticatedFetch).not.toHaveBeenCalledWith("/v1/sessions/conv_a1x/child_sessions");
  });
});
