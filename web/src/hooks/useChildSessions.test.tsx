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
