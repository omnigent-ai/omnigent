import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";

import type { WorkflowRun } from "@/lib/workflowRuns";

import {
  useCloneWorkflow,
  useSaveWorkflowPlan,
  useWorkflowRunDescendants,
  useWorkflowRuns,
} from "./useWorkflow";

// Let server data change without remounting or invalidating the real query.
vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

const COMPLETED_RUN = {
  id: "run_old",
  session_id: "session_old",
  title: "Completed run",
  started_at: 1,
  session_status: "idle",
  is_current: true,
  background_work_running: false,
  archived: false,
  progress: { total: 2, started: 0, done: 2, needs_input: 0, failed: 0 },
};

const RUNNING_RUN = {
  ...COMPLETED_RUN,
  id: "run_new",
  plan_graph_revision: "graph-3",
  session_id: "session_new",
  title: "New run",
  started_at: 2,
  session_status: "running",
  progress: { total: 2, started: 1, done: 0, needs_input: 0, failed: 0 },
};

function renderQuery<T>(hook: () => T) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(hook, { wrapper });
}

function renderRuns(workflowId = "workflow", sessionId?: string) {
  return renderQuery(() => useWorkflowRuns(workflowId, sessionId));
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: false });
  vi.mocked(authenticatedFetch).mockReset();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("useCloneWorkflow", () => {
  function renderClone() {
    const client = new QueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    return { ...renderHook(() => useCloneWorkflow(), { wrapper }), client };
  }

  it.each(["Review changes", "A".repeat(80), "📋".repeat(80)])(
    "copies the saved plan and chooses an available name within the limit: %s",
    async (name) => {
      const source = {
        id: "original",
        name,
        description: "Review the changes.",
        steps: [{ id: "review", name: "Review", goal: "Find bugs", depends_on: [] }],
        planning_session_id: "planning-session",
      };
      const copy = { ...source, id: "copy", planning_session_id: null };
      vi.mocked(authenticatedFetch)
        .mockResolvedValueOnce(Response.json(source))
        .mockResolvedValueOnce(Response.json({ detail: "Name taken" }, { status: 409 }))
        .mockResolvedValueOnce(Response.json(copy));
      const { result, client } = renderClone();
      client.setQueryData(["saved-workflows"], []);

      await act(async () => {
        await result.current.mutateAsync(source.id);
      });

      expect(authenticatedFetch).toHaveBeenNthCalledWith(1, "/v1/workflows/original");
      const writes = vi.mocked(authenticatedFetch).mock.calls.slice(1);
      expect(writes.map(([path]) => path)).toEqual(["/v1/workflows", "/v1/workflows"]);
      expect(writes.map(([, init]) => JSON.parse(init!.body as string))).toEqual([
        {
          name: [...name].slice(0, 73).join("") + " (copy)",
          description: source.description,
          steps: source.steps,
        },
        {
          name: [...name].slice(0, 71).join("") + " (copy 2)",
          description: source.description,
          steps: source.steps,
        },
      ]);
      expect(client.getQueryData(["workflow", copy.id])).toEqual(copy);
      expect(client.getQueryState(["saved-workflows"])?.isInvalidated).toBe(true);
    },
  );

  it.each(["read", "write"])("surfaces %s errors without retrying", async (operation) => {
    if (operation === "write") {
      vi.mocked(authenticatedFetch).mockResolvedValueOnce(
        Response.json({ name: "Review", description: "Review changes", steps: [] }),
      );
    }
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(
      Response.json({ detail: "Request failed" }, { status: 500 }),
    );
    const { result } = renderClone();

    await act(async () => {
      await expect(result.current.mutateAsync("original")).rejects.toThrow("Request failed");
    });
    expect(authenticatedFetch).toHaveBeenCalledTimes(operation === "read" ? 1 : 2);
  });
});

describe("useSaveWorkflowPlan", () => {
  it("saves only the steps when an older server refuses the description check", async () => {
    const steps = [{ id: "review", name: "Review", goal: null, depends_on: [] }];
    const saved = { id: "workflow", name: "Review", description: "Review changes.", steps };
    const proposal = {
      steps,
      description: "Reviews open changes.",
      expected_description: "Review changes.",
    };
    // How a server from before expected_description refuses the unknown field.
    const refusal = {
      detail: [{ type: "extra_forbidden", loc: ["body", "expected_description"] }],
    };
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(Response.json(refusal, { status: 422 }))
      .mockResolvedValueOnce(Response.json(saved));
    const client = new QueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useSaveWorkflowPlan("workflow"), { wrapper });

    await act(async () => {
      await expect(result.current.mutateAsync(proposal)).resolves.toEqual(saved);
    });
    const bodies = vi
      .mocked(authenticatedFetch)
      .mock.calls.map(([, init]) => JSON.parse(init!.body as string));
    expect(bodies).toEqual([proposal, { steps }]);
  });
});

describe("useWorkflowRuns live refresh", () => {
  it.each([
    { name: "empty", history: [] },
    { name: "completed", history: [COMPLETED_RUN] },
  ])(
    "discovers new runs with $name history and follows progress without remounting",
    async ({ history }) => {
      let serverRuns = history;
      vi.mocked(authenticatedFetch).mockImplementation(async () =>
        Response.json({ data: serverRuns, next_cursor: null }),
      );
      const { result, unmount } = renderRuns();
      await act(() => vi.advanceTimersByTimeAsync(1));
      expect(result.current.isSuccess).toBe(true);
      expect(result.current.data?.pages[0].runs.map((run) => run.id)).toEqual(
        history.map((run) => run.id),
      );

      serverRuns = [RUNNING_RUN, ...history];
      await act(() => vi.advanceTimersByTimeAsync(10_000));
      expect(result.current.data?.pages[0].runs.map((run) => run.id)).toEqual(
        serverRuns.map((run) => run.id),
      );
      expect(result.current.data?.pages[0].runs[0].sessionStatus).toBe("running");
      expect(result.current.data?.pages[0].runs[0].planGraphRevision).toBe("graph-3");

      serverRuns = [
        { ...RUNNING_RUN, session_status: "idle", background_work_running: true },
        ...history,
      ];
      await act(() => vi.advanceTimersByTimeAsync(10_000));
      expect(result.current.data?.pages[0].runs[0]).toMatchObject({
        sessionStatus: "idle",
        backgroundWorkRunning: true,
      });

      serverRuns = [
        { ...RUNNING_RUN, session_status: "idle", progress: COMPLETED_RUN.progress },
        ...history,
      ];
      await act(() => vi.advanceTimersByTimeAsync(10_000));
      expect(result.current.data?.pages[0].runs[0]).toMatchObject({
        sessionStatus: "idle",
        backgroundWorkRunning: false,
        progress: { done: 2 },
      });

      unmount();
      const callsAtUnmount = vi.mocked(authenticatedFetch).mock.calls.length;
      await act(() => vi.advanceTimersByTimeAsync(30_000));
      expect(authenticatedFetch).toHaveBeenCalledTimes(callsAtUnmount);
    },
  );

  it("does not poll without a workflow ID", async () => {
    renderRuns("");
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });
});

describe("useWorkflowRuns runs below a run", () => {
  it("maps who started each run and the runs below it, for one session when asked", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async () =>
      Response.json({
        data: [
          {
            ...RUNNING_RUN,
            workflow_id: "workflow",
            started_by: null,
            workflow_name: "Workflow",
            descendant_runs: [
              {
                ...COMPLETED_RUN,
                workflow_id: "called",
                workflow_name: null,
                started_by: { session_id: "session_new", title: "New run" },
              },
            ],
            descendant_run_count: 3,
            descendant_runs_cursor: "session_old",
          },
        ],
        next_cursor: null,
      }),
    );
    const { result } = renderRuns("workflow", "session_new");
    await act(() => vi.advanceTimersByTimeAsync(1));

    const url = new URL(String(vi.mocked(authenticatedFetch).mock.calls[0]?.[0]), "http://x");
    expect(url.searchParams.get("session_id")).toBe("session_new");
    const [run] = result.current.data?.pages[0].runs ?? [];
    expect(run).toMatchObject({
      workflowId: "workflow",
      workflowName: "Workflow",
      startedBy: undefined,
      descendantRunCount: 3,
      descendantRunsCursor: "session_old",
    });
    expect(run?.descendantRuns).toEqual([
      expect.objectContaining({
        id: "run_old",
        workflowId: "called",
        workflowName: null,
        startedBy: { sessionId: "session_new", title: "New run" },
        descendantRuns: [],
        descendantRunsCursor: null,
      }),
    ]);
  });

  it("continues a run's descendant runs from its cursor once enabled", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async () =>
      Response.json({ data: [{ ...COMPLETED_RUN, workflow_name: "Called" }], next_cursor: "more" }),
    );
    const run = {
      id: "run_new",
      sessionId: "session_new",
      descendantRunsCursor: "session_x",
    } as WorkflowRun;
    const idle = renderQuery(() => useWorkflowRunDescendants("workflow", run, false));
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(authenticatedFetch).not.toHaveBeenCalled();
    idle.unmount();

    const { result } = renderQuery(() => useWorkflowRunDescendants("workflow", run, true));
    await act(() => vi.advanceTimersByTimeAsync(1));
    const url = new URL(String(vi.mocked(authenticatedFetch).mock.calls[0]?.[0]), "http://x");
    expect(url.pathname).toBe("/v1/workflows/workflow/runs/run_new/descendant_runs");
    expect(url.searchParams.get("session_id")).toBe("session_new");
    expect(url.searchParams.get("after")).toBe("session_x");
    expect(result.current.data?.pages[0]).toMatchObject({
      runs: [expect.objectContaining({ id: "run_old", workflowName: "Called" })],
      nextCursor: "more",
    });
    expect(result.current.hasNextPage).toBe(true);
  });
});
