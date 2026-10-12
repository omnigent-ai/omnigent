import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, waitFor } from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import { useSession } from "@/hooks/useSession";
import { SubagentsGraphView } from "./SubagentsGraphView";

// ReactFlow needs a measured canvas and exhausts jsdom; the wiring under test
// is the hook/context boundary around it, so the canvas is a plain container.
vi.mock("@xyflow/react", () => ({
  ReactFlow: ({ children }: PropsWithChildren) => <div data-testid="react-flow">{children}</div>,
  Background: () => null,
  Handle: () => null,
  Position: { Top: "top", Bottom: "bottom", Left: "left", Right: "right" },
  useReactFlow: () => ({ zoomIn: vi.fn(), zoomOut: vi.fn(), fitView: vi.fn() }),
}));
vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/hooks/useSession", () => ({ useSession: vi.fn() }));

function descendantRow(id: string, parentId: string, depth: number) {
  return {
    id,
    parent_session_id: parentId,
    depth,
    title: `worker:${id}`,
    tool: "worker",
    session_name: id,
    current_task_status: null,
    busy: false,
  };
}

beforeEach(() => {
  vi.mocked(authenticatedFetch).mockReset();
  vi.mocked(useSession).mockReturnValue({
    session: {
      id: "conv_root",
      agentId: "ag_root",
      agentName: null,
      runnerId: null,
      status: "idle",
      createdAt: 0,
      title: null,
      labels: {},
      items: [],
      pendingElicitations: [],
      permissionLevel: 4,
      parentSessionId: null,
      subAgentName: null,
      kind: "default",
    },
    isLoading: false,
    error: null,
  });
});

describe("SubagentsGraphView request wiring", () => {
  it("loads a three-level tree from the subtree feed without per-node requests", async () => {
    // WHY: the graph collects children per node through the same hook the
    // list uses; rendered under the subtree context, no collector may fetch
    // its own child_sessions page.
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          object: "list",
          data: [
            descendantRow("conv_a1x", "conv_a1", 3),
            descendantRow("conv_a1", "conv_a", 2),
            descendantRow("conv_b", "conv_root", 1),
            descendantRow("conv_a", "conv_root", 1),
          ],
          last_id: "conv_a",
          has_more: false,
        }),
      ),
    );
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/c/conv_root"]}>
          <SubagentsGraphView conversationId="conv_root" rootSessionId="conv_root" />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await waitFor(() =>
      expect(queryClient.getQueryData(["conversation", "conv_a1", "child_sessions"])).toBeDefined(),
    );
    // Give any per-node fetch a chance to fire before counting.
    await new Promise((resolve) => {
      setTimeout(resolve, 50);
    });
    const urls = vi.mocked(authenticatedFetch).mock.calls.map(([url]) => String(url));
    expect(urls).toEqual(["/v1/sessions/conv_root/descendants?max_depth=3&limit=1000"]);
  });
});
