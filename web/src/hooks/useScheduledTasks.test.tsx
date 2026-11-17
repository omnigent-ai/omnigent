// Tests for the scheduled-tasks React-Query hooks: the list query shape, the
// invalidate-on-success contract of the create / update / delete mutations, and
// the status-aware run-history refetchInterval that drives a run row to its
// terminal state live.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/lib/scheduledTasksApi";
import {
  hasUnsettledRun,
  SCHEDULED_TASKS_KEY,
  scheduledTaskKey,
  scheduledTaskRunsKey,
  useCreateScheduledTask,
  useDeleteScheduledTask,
  useRunScheduledTaskNow,
  useScheduledTask,
  useScheduledTaskRunPullRequests,
  useScheduledTaskRuns,
  useScheduledTaskRunSteps,
  useScheduledTasks,
  useSharedScheduledTasks,
  useUpdateScheduledTask,
} from "./useScheduledTasks";

vi.mock("@/lib/scheduledTasksApi", () => ({
  listScheduledTasks: vi.fn(),
  listSharedScheduledTasks: vi.fn(),
  getScheduledTask: vi.fn(),
  listScheduledTaskRuns: vi.fn(),
  listScheduledTaskRunSteps: vi.fn(),
  listScheduledTaskRunPullRequests: vi.fn(),
  createScheduledTask: vi.fn(),
  updateScheduledTask: vi.fn(),
  deleteScheduledTask: vi.fn(),
  runScheduledTaskNow: vi.fn(),
}));

const TASK: api.ScheduledTask = {
  id: "st_1",
  name: "Nightly triage",
  prompt: "Triage",
  rrule: "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
  ownerUserId: null,
  agentId: "ag_1",
  timezone: "UTC",
  createdAt: 1,
  updatedAt: 2,
  modelOverride: null,
  reasoningEffort: null,
  permissionMode: null,
  mcpServices: [],
  sessionMode: "new",
  sessionId: null,
  workspace: null,
  hostId: null,
  executionTarget: "connected_host",
  projectId: null,
  state: "active",
  lastRunAt: null,
  lastRunStatus: null,
  lastRunConversationId: null,
  nextRunAt: null,
};

function makeWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
  return { queryClient, wrapper };
}

const RUN: api.ScheduledTaskRun = {
  sessionCostUsd: null,
  id: "run_1",
  scheduledTaskId: "st_1",
  status: "running",
  scheduledAt: 1_700_000_000,
  conversationId: "c_1",
  firedAt: 1_700_000_000,
  finishedAt: null,
  errorCode: null,
  errorDetail: null,
  conversationUpdatedAt: 1_700_000_500,
  conversationStatus: "running",
  viewerLastSeen: null,
  viewerUnread: false,
};

beforeEach(() => {
  vi.mocked(api.listScheduledTasks).mockResolvedValue([TASK]);
  vi.mocked(api.listScheduledTaskRuns).mockResolvedValue([]);
  vi.mocked(api.createScheduledTask).mockResolvedValue(TASK);
  vi.mocked(api.updateScheduledTask).mockResolvedValue({ ...TASK, state: "paused" });
  vi.mocked(api.deleteScheduledTask).mockResolvedValue(undefined);
  vi.mocked(api.runScheduledTaskNow).mockResolvedValue("run_1");
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("useScheduledTasks", () => {
  it("returns the list from the API", async () => {
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useScheduledTasks(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([TASK]);
  });
});

describe("useSharedScheduledTasks", () => {
  const SHARED = { ...TASK, id: "st_shared", viewerRole: "viewer" as const };

  it("pages by cursor and paints the shared task's detail from its cache", async () => {
    vi.mocked(api.listSharedScheduledTasks)
      .mockResolvedValueOnce({ tasks: [], nextCursor: "st_0" })
      .mockResolvedValueOnce({ tasks: [SHARED], nextCursor: null });
    let resolveDetail: (task: api.ScheduledTask) => void = () => {};
    vi.mocked(api.getScheduledTask).mockReturnValue(
      new Promise((resolve) => {
        resolveDetail = resolve;
      }),
    );
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useSharedScheduledTasks(true), { wrapper });
    await waitFor(() => expect(result.current.hasNextPage).toBe(true));
    expect(api.listSharedScheduledTasks).toHaveBeenLastCalledWith(null);
    await act(() => result.current.fetchNextPage());
    await waitFor(() => expect(result.current.hasNextPage).toBe(false));
    expect(api.listSharedScheduledTasks).toHaveBeenLastCalledWith("st_0");
    expect(result.current.data?.pages.flatMap((page) => page.tasks)).toEqual([SHARED]);

    const detail = renderHook(() => useScheduledTask("st_shared"), { wrapper });
    expect(detail.result.current.data).toEqual(SHARED);
    resolveDetail(SHARED);
  });

  it("does not fetch while disabled", () => {
    vi.mocked(api.listSharedScheduledTasks).mockClear();
    const { wrapper } = makeWrapper();
    renderHook(() => useSharedScheduledTasks(false), { wrapper });
    expect(api.listSharedScheduledTasks).not.toHaveBeenCalled();
  });
});

describe("mutations invalidate the list", () => {
  it("create invalidates SCHEDULED_TASKS_KEY", async () => {
    const { queryClient, wrapper } = makeWrapper();
    const spy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useCreateScheduledTask(), { wrapper });
    await result.current.mutateAsync({
      name: "n",
      prompt: "p",
      rrule: "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
      agentId: "ag_1",
    });
    expect(spy).toHaveBeenCalledWith({ queryKey: SCHEDULED_TASKS_KEY });
  });

  it("update invalidates the list", async () => {
    const { queryClient, wrapper } = makeWrapper();
    const spy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useUpdateScheduledTask(), { wrapper });
    await result.current.mutateAsync({ id: "st_1", input: { state: "paused" } });
    expect(spy).toHaveBeenCalledWith({ queryKey: SCHEDULED_TASKS_KEY });
  });

  it("delete invalidates the list", async () => {
    const { queryClient, wrapper } = makeWrapper();
    const spy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useDeleteScheduledTask(), { wrapper });
    await result.current.mutateAsync("st_1");
    expect(spy).toHaveBeenCalledWith({ queryKey: SCHEDULED_TASKS_KEY });
    expect(spy).not.toHaveBeenCalledWith({ queryKey: ["conversations"] });
  });

  // Either can stop the automation managing its session's sharing, which the
  // session's grants list reports.
  it("update and delete refresh every session's grants", async () => {
    const { queryClient, wrapper } = makeWrapper();
    const spy = vi.spyOn(queryClient, "invalidateQueries");
    const update = renderHook(() => useUpdateScheduledTask(), { wrapper });
    await update.result.current.mutateAsync({ id: "st_1", input: { sessionMode: "new" } });
    expect(spy).toHaveBeenCalledWith({ queryKey: ["permissions"] });

    spy.mockClear();
    const remove = renderHook(() => useDeleteScheduledTask(), { wrapper });
    await remove.result.current.mutateAsync("st_1");
    expect(spy).toHaveBeenCalledWith({ queryKey: ["permissions"] });
  });

  it("deleting an automation that runs from a session refreshes the cached sessions", async () => {
    const { queryClient, wrapper } = makeWrapper();
    queryClient.setQueryData(SCHEDULED_TASKS_KEY, [{ ...TASK, sourceSessionId: "conv_copy" }]);
    const spy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useDeleteScheduledTask(), { wrapper });
    await result.current.mutateAsync(TASK.id);
    // The server cleared the copy's automation label; the sidebar must refetch it.
    for (const queryKey of [["conversations"], ["project-sessions"], ["session", "conv_copy"]]) {
      expect(spy).toHaveBeenCalledWith({ queryKey });
    }
  });

  it("a deleted task's detail page refetches instead of rendering from cache", async () => {
    const { queryClient, wrapper } = makeWrapper();
    vi.mocked(api.getScheduledTask).mockResolvedValue(TASK);
    const detail = renderHook(() => useScheduledTask(TASK.id), { wrapper });
    await waitFor(() => expect(detail.result.current.data).toEqual(TASK));
    queryClient.setQueryData(SCHEDULED_TASKS_KEY, [TASK]);
    detail.unmount();

    const { result } = renderHook(() => useDeleteScheduledTask(), { wrapper });
    await result.current.mutateAsync(TASK.id);

    // Back to the detail page: nothing cached to show, so it asks the server.
    vi.mocked(api.getScheduledTask).mockRejectedValue(new Error("not found"));
    const back = renderHook(() => useScheduledTask(TASK.id), { wrapper });
    expect(back.result.current.data).toBeUndefined();
    await waitFor(() => expect(back.result.current.isError).toBe(true));
    expect(queryClient.getQueryData(SCHEDULED_TASKS_KEY)).toEqual([]);
  });

  it("run-now calls the API and immediately invalidates the list, detail + runs", async () => {
    const { queryClient, wrapper } = makeWrapper();
    queryClient.setQueryData(scheduledTaskRunsKey("st_1"), [RUN]);
    const runs = renderHook(() => useScheduledTaskRuns("st_1"), { wrapper });
    expect(runs.result.current.isFetchedAfterMount).toBe(false);
    expect(api.listScheduledTaskRuns).not.toHaveBeenCalled();
    const spy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useRunScheduledTaskNow(), { wrapper });
    expect(await result.current.mutateAsync("st_1")).toBe("run_1");
    await waitFor(() => expect(runs.result.current.isFetchedAfterMount).toBe(true));
    expect(api.listScheduledTaskRuns).toHaveBeenCalledTimes(1);
    expect(api.runScheduledTaskNow).toHaveBeenCalledWith("st_1");
    // Refreshes the list (so the completion badge updates)…
    expect(spy).toHaveBeenCalledWith({ queryKey: SCHEDULED_TASKS_KEY });
    // …the detail query…
    expect(spy).toHaveBeenCalledWith({ queryKey: scheduledTaskKey("st_1") });
    // …and the fired task's run history, so the new "running" row appears at
    // once. Driving it to terminal is the refetchInterval's job, not this
    // mutation's — it must NOT start a poll of its own.
    expect(spy).toHaveBeenCalledWith({ queryKey: scheduledTaskRunsKey("st_1") });
  });

  it("run-now does NOT schedule its own follow-up refetches (no accelerated poll)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    try {
      const { queryClient, wrapper } = makeWrapper();
      const refetchSpy = vi.spyOn(queryClient, "refetchQueries");
      const { result } = renderHook(() => useRunScheduledTaskNow(), { wrapper });

      await act(async () => {
        await result.current.mutateAsync("st_1");
      });
      // Well past the old ~20s poll budget: nothing self-scheduled fires.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });

      expect(countRunRefetches(refetchSpy)).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });
});

// Run row running, conversation running (fresh fire, nothing settled yet).
const RUNNING_RUN: api.ScheduledTaskRun = {
  ...RUN,
  status: "running",
  conversationStatus: "running",
};
// Run row terminal but the CONVERSATION is still running — the intermediate
// state a run-status-only check would treat as settled one step too early.
const SUCCEEDED_CONV_RUNNING_RUN: api.ScheduledTaskRun = {
  ...RUN,
  status: "succeeded",
  conversationStatus: "running",
};
// Fully settled: run terminal AND conversation left "running".
const SUCCEEDED_RUN: api.ScheduledTaskRun = {
  ...RUN,
  status: "succeeded",
  conversationStatus: "idle",
};
// Host-less skipped run: terminal with no conversation — nothing to wait for.
const SKIPPED_NO_CONV_RUN: api.ScheduledTaskRun = {
  ...RUN,
  status: "skipped",
  conversationId: null,
  conversationStatus: null,
};

function countRunRefetches(spy: { mock: { calls: unknown[][] } }) {
  return spy.mock.calls.filter(
    (args) =>
      JSON.stringify(args[0]) === JSON.stringify({ queryKey: scheduledTaskRunsKey("st_1") }),
  ).length;
}

describe("hasUnsettledRun (the refetchInterval stop condition)", () => {
  it("is true while a run row is still scheduled, starting or running", () => {
    expect(hasUnsettledRun([RUNNING_RUN])).toBe(true);
    expect(hasUnsettledRun([{ ...RUN, status: "scheduled", conversationStatus: null }])).toBe(true);
    expect(hasUnsettledRun([{ ...RUN, status: "starting", conversationStatus: null }])).toBe(true);
  });

  it("stays true while the run row is terminal but its CONVERSATION still runs", () => {
    // The unread dot keys off the conversation, not the row — stopping here
    // would leave the dot grey until a remount refetched the idle conversation.
    expect(hasUnsettledRun([SUCCEEDED_CONV_RUNNING_RUN])).toBe(true);
  });

  it("is false once everything is terminal", () => {
    expect(hasUnsettledRun([SUCCEEDED_RUN])).toBe(false);
    expect(hasUnsettledRun([{ ...RUN, status: "failed", conversationStatus: "idle" }])).toBe(false);
  });

  it("is false for a terminal run with NO conversation (host-less skip)", () => {
    expect(hasUnsettledRun([SKIPPED_NO_CONV_RUN])).toBe(false);
  });

  it("is false for no-data / empty history rather than polling forever", () => {
    expect(hasUnsettledRun(undefined)).toBe(false);
    expect(hasUnsettledRun([])).toBe(false);
  });

  it("treats an unrecognised status as settled so polling can never strand", () => {
    // `incomplete` is an END state (the server's stale-run force-fail backstop).
    // Any status this client doesn't know must not keep the interval alive.
    expect(hasUnsettledRun([{ ...RUN, status: "incomplete", conversationStatus: "idle" }])).toBe(
      false,
    );
  });

  it("is true when ANY run in the history is unsettled, not just the newest", () => {
    expect(hasUnsettledRun([SUCCEEDED_RUN, { ...RUNNING_RUN, id: "run_old" }])).toBe(true);
  });
});

describe("useScheduledTaskRuns live refresh", () => {
  it("refreshes steps when a run is replaced or its settled session advances", async () => {
    const { wrapper } = makeWrapper();
    const previous = Array.from({ length: 20 }, (_, index) => ({
      ...SUCCEEDED_RUN,
      id: `run-old-${index}`,
    }));
    const current = [{ ...SUCCEEDED_RUN, id: "run-new" }, ...previous.slice(0, 19)];
    const stepsFor = (runs: api.ScheduledTaskRun[]) =>
      runs.map((run) => ({
        runId: run.id,
        planId: `plan-${run.id}`,
        isCurrent: true,
        workflowId: null,
        planName: null,
        steps: [],
        statuses: [],
      }));
    vi.mocked(api.listScheduledTaskRunSteps)
      .mockResolvedValueOnce(stepsFor(previous))
      .mockResolvedValueOnce(stepsFor(current));
    const { result, rerender } = renderHook(
      ({ runs }) => useScheduledTaskRunSteps("st_1", runs, true),
      { wrapper, initialProps: { runs: previous } },
    );
    await waitFor(() => expect(result.current.has("run-old-0")).toBe(true));
    rerender({ runs: current });
    await waitFor(() => expect(result.current.has("run-new")).toBe(true));
    expect(api.listScheduledTaskRunSteps).toHaveBeenCalledTimes(2);
    const working = [{ ...RUNNING_RUN, id: "run-working" }, ...current.slice(0, 19)];
    vi.mocked(api.listScheduledTaskRunSteps).mockResolvedValueOnce(stepsFor(working));
    rerender({ runs: working });
    await waitFor(() => expect(result.current.has("run-working")).toBe(true));
    const finished: api.ScheduledTaskRunSteps = {
      ...stepsFor(working)[0]!,
      workflowId: "a".repeat(32),
      steps: [{ id: "check", name: "Check results", goal: null, depends_on: [] }],
      statuses: [
        {
          id: "status-started",
          position: 1,
          created_at: 1_700_000_500,
          scheduled_task_run_id: "run-working",
          step: "check",
          author: { type: "agent" },
          kind: "status",
          content: { status: "started" },
        },
      ],
    };
    const settled = [{ ...SUCCEEDED_RUN, id: "run-working" }, ...current.slice(0, 19)];
    vi.mocked(api.listScheduledTaskRunSteps).mockResolvedValueOnce([finished]);
    rerender({ runs: settled });
    await waitFor(() => expect(result.current.get("run-working")).toEqual(finished));
    expect(api.listScheduledTaskRunSteps).toHaveBeenCalledTimes(4);

    // A short continuation can finish between history polls; a visible busy/idle
    // cycle must also avoid restoring the previous, still-fresh idle snapshot.
    const refreshSession = async (conversationStatus: string, position: number) => {
      const updatedAt = 1_700_000_510 + position;
      const refreshed: api.ScheduledTaskRunSteps = {
        ...finished,
        statuses: [
          {
            ...finished.statuses[0]!,
            id: `status-${position}`,
            position,
            created_at: updatedAt,
            content: { status: conversationStatus === "running" ? "started" : "done" },
          },
        ],
      };
      vi.mocked(api.listScheduledTaskRunSteps).mockResolvedValueOnce([refreshed]);
      rerender({
        runs: [
          { ...settled[0]!, conversationStatus, conversationUpdatedAt: updatedAt },
          ...settled.slice(1),
        ],
      });
      await waitFor(() => expect(result.current.get("run-working")).toEqual(refreshed));
    };
    await refreshSession("idle", 2);
    await refreshSession("running", 3);
    await refreshSession("idle", 4);
    expect(api.listScheduledTaskRunSteps).toHaveBeenCalledTimes(7);
  });

  it("asks for no steps while disabled, even with a run in flight", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    try {
      const { wrapper } = makeWrapper();
      // A viewer's history: in flight, with none of its session's data.
      const runs = [{ ...RUNNING_RUN, conversationStatus: null, conversationUpdatedAt: null }];
      vi.mocked(api.listScheduledTaskRunSteps).mockResolvedValueOnce([]);
      const { rerender } = renderHook(
        ({ enabled }) => useScheduledTaskRunSteps("st_1", runs, enabled),
        { wrapper, initialProps: { enabled: false } },
      );
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });
      expect(api.listScheduledTaskRunSteps).not.toHaveBeenCalled();

      rerender({ enabled: true });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(api.listScheduledTaskRunSteps).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps refetching a >20s run until it settles, WITHOUT a remount", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    try {
      const { wrapper } = makeWrapper();
      // A realistically SLOW run: still running well past the old 20s poll
      // budget (which is exactly why the row used to spin forever), settling
      // only at ~60s. Each phase must be observed live by the same mount.
      vi.mocked(api.listScheduledTaskRuns).mockResolvedValue([RUNNING_RUN]);

      const { result } = renderHook(() => useScheduledTaskRuns("st_1"), { wrapper });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(result.current.isSuccess).toBe(true);

      const callsAtStart = vi.mocked(api.listScheduledTaskRuns).mock.calls.length;

      // Past the OLD 20s budget the run is still going — the interval must not
      // have given up.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });
      const callsAt30s = vi.mocked(api.listScheduledTaskRuns).mock.calls.length;
      expect(callsAt30s).toBeGreaterThan(callsAtStart);
      // Still spinning on-screen, which is correct — the run really is running.
      expect(result.current.data).toEqual([RUNNING_RUN]);

      // The row goes terminal while its conversation is still running: keep going.
      vi.mocked(api.listScheduledTaskRuns).mockResolvedValue([SUCCEEDED_CONV_RUNNING_RUN]);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(12_000);
      });
      const callsAfterRowTerminal = vi.mocked(api.listScheduledTaskRuns).mock.calls.length;
      expect(callsAfterRowTerminal).toBeGreaterThan(callsAt30s);

      // Conversation settles too — the row is now fully terminal on-screen, with
      // no navigation away and back (the bug being fixed).
      vi.mocked(api.listScheduledTaskRuns).mockResolvedValue([SUCCEEDED_RUN]);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(12_000);
      });
      expect(result.current.data).toEqual([SUCCEEDED_RUN]);

      // …and polling drops to the idle cadence: a minute brings one or two
      // requests, where the in-flight cadence would make fifteen.
      const callsAfterSettled = vi.mocked(api.listScheduledTaskRuns).mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000);
      });
      const idleCalls = vi.mocked(api.listScheduledTaskRuns).mock.calls.length - callsAfterSettled;
      expect(idleCalls).toBeGreaterThanOrEqual(1);
      expect(idleCalls).toBeLessThanOrEqual(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps a slow poll while the history is settled, so a later scheduled run appears", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    try {
      const { wrapper } = makeWrapper();
      vi.mocked(api.listScheduledTaskRuns).mockResolvedValue([SUCCEEDED_RUN]);

      const { result } = renderHook(() => useScheduledTaskRuns("st_1"), { wrapper });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(result.current.data).toEqual([SUCCEEDED_RUN]);

      // No fast polling while everything is settled.
      const callsAfterFirstFetch = vi.mocked(api.listScheduledTaskRuns).mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(20_000);
      });
      expect(vi.mocked(api.listScheduledTaskRuns).mock.calls.length).toBe(callsAfterFirstFetch);

      // The schedule fires while the page stays open: the slow poll finds it.
      vi.mocked(api.listScheduledTaskRuns).mockResolvedValue([RUNNING_RUN, SUCCEEDED_RUN]);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(11_000);
      });
      expect(result.current.data).toEqual([RUNNING_RUN, SUCCEEDED_RUN]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops refetching once the page unmounts", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    try {
      const { wrapper } = makeWrapper();
      // Never settles — only the unmount can stop this.
      vi.mocked(api.listScheduledTaskRuns).mockResolvedValue([RUNNING_RUN]);

      const { result, unmount } = renderHook(() => useScheduledTaskRuns("st_1"), { wrapper });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(result.current.isSuccess).toBe(true);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000);
      });

      unmount();
      const callsAtUnmount = vi.mocked(api.listScheduledTaskRuns).mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000);
      });
      expect(vi.mocked(api.listScheduledTaskRuns).mock.calls.length).toBe(callsAtUnmount);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps polling while awaitingRun even though the history is all-terminal", async () => {
    // REGRESSION: `POST /run` returns 202 and the server writes the run row a few
    // seconds LATER, so the invalidate right after a fire sees a history that is
    // still entirely terminal. Keying the interval on run status alone made it
    // evaluate to `false` at that moment and never restart — the new row never
    // appeared at all.
    vi.useFakeTimers({ shouldAdvanceTime: false });
    try {
      const { queryClient, wrapper } = makeWrapper();
      vi.mocked(api.listScheduledTaskRuns).mockResolvedValue([SUCCEEDED_RUN]);

      const { result } = renderHook(() => useScheduledTaskRuns("st_1", true, true), { wrapper });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(result.current.isSuccess).toBe(true);

      const callsAtStart = vi.mocked(api.listScheduledTaskRuns).mock.calls.length;
      // The fired row lands a few seconds in; polling must still be running.
      vi.mocked(api.listScheduledTaskRuns).mockResolvedValue([
        { ...RUNNING_RUN, id: "run_new" },
        SUCCEEDED_RUN,
      ]);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000);
      });

      // Polling kept going despite the all-terminal history, and picked the new
      // row up. Asserted on the cache the poll writes rather than the rendered
      // value, which lags it by a tick under fake timers.
      expect(vi.mocked(api.listScheduledTaskRuns).mock.calls.length).toBeGreaterThan(callsAtStart);
      const cached = queryClient.getQueryData<api.ScheduledTaskRun[]>(scheduledTaskRunsKey("st_1"));
      expect(cached?.[0]?.id).toBe("run_new");
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not fetch at all while disabled", async () => {
    const { wrapper } = makeWrapper();
    vi.mocked(api.listScheduledTaskRuns).mockResolvedValue([RUNNING_RUN]);
    renderHook(() => useScheduledTaskRuns("st_1", false), { wrapper });
    expect(api.listScheduledTaskRuns).not.toHaveBeenCalled();
  });
});

describe("useScheduledTaskRunPullRequests", () => {
  const pullRequest = (state: string | null): api.ScheduledTaskRunPullRequest => ({
    url: "https://github.com/example/repo/pull/1",
    provider: "github",
    repository: "example/repo",
    number: 1,
    relationship: "created",
    state,
  });
  const page = (
    states: (string | null)[],
    refreshPending = false,
  ): api.ScheduledTaskRunPullRequestsPage => ({
    runs: states.length ? [{ runId: "run_1", pullRequests: states.map(pullRequest) }] : [],
    refreshPending,
  });
  // Settled long ago, so a late PR report can't still arrive.
  const runs = [{ ...SUCCEEDED_RUN, id: "run_1" }];
  const tick = (ms: number) =>
    act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  const cachedStates = (queryClient: QueryClient) =>
    queryClient
      .getQueriesData<api.ScheduledTaskRunPullRequestsPage>({
        queryKey: ["scheduled-task-run-pull-requests", "st_1"],
      })[0]?.[1]
      ?.runs.flatMap((run) => run.pullRequests.map((pr) => pr.state));

  it("asks again each minute only while a settled run has an open or unknown PR", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    try {
      const { wrapper, queryClient } = makeWrapper();
      vi.mocked(api.listScheduledTaskRunPullRequests)
        .mockResolvedValueOnce(page(["OPEN"]))
        .mockResolvedValueOnce(page([null]))
        .mockResolvedValue(page(["MERGED"]));
      const { result } = renderHook(() => useScheduledTaskRunPullRequests("st_1", runs, true), {
        wrapper,
      });
      await tick(0);
      expect(result.current.get("run_1")?.pullRequests[0]?.state).toBe("OPEN");
      expect(api.listScheduledTaskRunPullRequests).toHaveBeenCalledWith("st_1", { limit: 20 });
      // An unknown state keeps asking too.
      await tick(60_000);
      expect(cachedStates(queryClient)).toEqual([null]);
      await tick(60_000);
      expect(cachedStates(queryClient)).toEqual(["MERGED"]);
      // Every PR has settled, so nothing asks again.
      await tick(180_000);
      expect(api.listScheduledTaskRunPullRequests).toHaveBeenCalledTimes(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it("finds PRs a background refresh stores after an empty answer, with the runs unchanged", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    try {
      const { wrapper, queryClient } = makeWrapper();
      vi.mocked(api.listScheduledTaskRunPullRequests)
        .mockResolvedValueOnce(page([], true))
        .mockResolvedValue(page(["MERGED"]));
      renderHook(() => useScheduledTaskRunPullRequests("st_1", runs, true), { wrapper });
      await tick(0);
      expect(cachedStates(queryClient)).toEqual([]);
      await tick(60_000);
      expect(cachedStates(queryClient)).toEqual(["MERGED"]);
      await tick(180_000);
      expect(api.listScheduledTaskRunPullRequests).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps asking for a few minutes after a run finishes, for its last PR report", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    try {
      const { wrapper } = makeWrapper();
      const finished = [
        { ...SUCCEEDED_RUN, id: "run_1", finishedAt: Math.floor(Date.now() / 1000) },
      ];
      vi.mocked(api.listScheduledTaskRunPullRequests).mockResolvedValue(page([]));
      renderHook(() => useScheduledTaskRunPullRequests("st_1", finished, true), { wrapper });
      await tick(0);
      await tick(4 * 60_000);
      const asked = vi.mocked(api.listScheduledTaskRunPullRequests).mock.calls.length;
      expect(asked).toBeGreaterThanOrEqual(4);
      await tick(5 * 60_000);
      expect(vi.mocked(api.listScheduledTaskRunPullRequests).mock.calls.length).toBeLessThanOrEqual(
        asked + 1,
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("asks for nothing while disabled", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    try {
      const { wrapper } = makeWrapper();
      renderHook(() => useScheduledTaskRunPullRequests("st_1", [RUNNING_RUN], false), {
        wrapper,
      });
      await tick(30_000);
      expect(api.listScheduledTaskRunPullRequests).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});
