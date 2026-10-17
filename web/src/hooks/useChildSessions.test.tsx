import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import {
  childSessionsQueryKey,
  useChildSessions,
  useDescendantSession,
  type ChildSessionInfo,
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
  it("keeps a newer child lifecycle when an in-flight HTTP snapshot arrives late", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let release!: (response: Response) => void;
    vi.mocked(authenticatedFetch).mockReturnValueOnce(
      new Promise((resolve) => {
        release = resolve;
      }),
    );
    const { result } = renderHook(() => useChildSessions("parent"), {
      wrapper: ({ children }: PropsWithChildren) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      ),
    });
    const observation = {
      runner_id: "B",
      binding_epoch: 2,
      revision: 5,
      observation_id: "new",
      status: "running",
      response_id: "new_turn",
      error_key: null,
    };
    const child: ChildSessionInfo = {
      id: "child",
      title: "Child",
      task_summary: null,
      tool: null,
      session_name: null,
      busy: true,
      current_task_status: "in_progress",
      last_task_error: null,
      last_message_preview: "Current work",
      pending_elicitations_count: 1,
      session_observation: observation,
      labels: { project: "old" },
    };
    act(() => client.setQueryData(childSessionsQueryKey("parent"), [child]));
    await act(async () =>
      release(
        new Response(
          JSON.stringify({
            object: "list",
            data: [
              {
                ...child,
                title: "New title",
                busy: false,
                current_task_status: "failed",
                last_task_error: { code: "runner_disconnected", message: "old runner" },
                session_observation: { ...observation, binding_epoch: 1, revision: 4 },
                runner_offline_episode_id: "old_outage",
                last_message_preview: "Old failure",
                labels: { project: "new", "omnigent.last_task_error_code": "runner_disconnected" },
                pending_elicitations_count: 0,
              },
            ],
          }),
        ),
      ),
    );
    await waitFor(() => expect(result.current.children[0]?.title).toBe("New title"));
    expect(result.current.children[0]).toMatchObject({
      busy: true,
      current_task_status: "in_progress",
      last_task_error: null,
      session_observation: observation,
      last_message_preview: "Current work",
      pending_elicitations_count: 1,
      labels: { project: "new" },
    });
    expect(result.current.children[0].labels).not.toHaveProperty("omnigent.last_task_error_code");
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
    expect(authenticatedFetch).toHaveBeenCalledWith("/v1/sessions/conv_parent/child_sessions");
  });
});
