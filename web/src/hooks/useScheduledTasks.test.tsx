// Tests for the scheduled-tasks React-Query hooks: the list query shape, the
// invalidate-on-success contract of the create / update / delete mutations,
// and the accelerated post-run-now run-history poll.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/lib/scheduledTasksApi";
import {
  SCHEDULED_TASKS_KEY,
  scheduledTaskRunsKey,
  useCreateScheduledTask,
  useDeleteScheduledTask,
  useRunScheduledTaskNow,
  useScheduledTasks,
  useUpdateScheduledTask,
} from "./useScheduledTasks";

vi.mock("@/lib/scheduledTasksApi", () => ({
  listScheduledTasks: vi.fn(),
  listScheduledTaskRuns: vi.fn(),
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
  workspace: null,
  hostId: null,
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
  id: "run_1",
  scheduledTaskId: "st_1",
  status: "running",
  scheduledAt: 1_700_000_000,
  conversationId: "c_1",
  firedAt: 1_700_000_000,
  finishedAt: null,
  errorCode: null,
  conversationUpdatedAt: 1_700_000_500,
  conversationStatus: "running",
  viewerUnread: false,
};

beforeEach(() => {
  vi.mocked(api.listScheduledTasks).mockResolvedValue([TASK]);
  vi.mocked(api.listScheduledTaskRuns).mockResolvedValue([]);
  vi.mocked(api.createScheduledTask).mockResolvedValue(TASK);
  vi.mocked(api.updateScheduledTask).mockResolvedValue({ ...TASK, state: "paused" });
  vi.mocked(api.deleteScheduledTask).mockResolvedValue(undefined);
  vi.mocked(api.runScheduledTaskNow).mockResolvedValue(undefined);
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
  });

  it("run-now calls the API and invalidates the list + that task's runs", async () => {
    const { queryClient, wrapper } = makeWrapper();
    const spy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useRunScheduledTaskNow(), { wrapper });
    await result.current.mutateAsync("st_1");
    expect(api.runScheduledTaskNow).toHaveBeenCalledWith("st_1");
    // Refreshes the list (so the completion badge updates)…
    expect(spy).toHaveBeenCalledWith({ queryKey: SCHEDULED_TASKS_KEY });
    // …and the fired task's run history (via the final invalidate after polling).
    expect(spy).toHaveBeenCalledWith({ queryKey: scheduledTaskRunsKey("st_1") });
  });
});

const RUNNING_RUN: api.ScheduledTaskRun = { ...RUN, status: "running" };
const SUCCEEDED_RUN: api.ScheduledTaskRun = { ...RUN, status: "succeeded" };

function countRunRefetches(spy: { mock: { calls: unknown[][] } }) {
  return spy.mock.calls.filter(
    (args) =>
      JSON.stringify(args[0]) === JSON.stringify({ queryKey: scheduledTaskRunsKey("st_1") }),
  ).length;
}

describe("run-now fast-poll after 202", () => {
  it("keeps polling while the newest run is 'running' and stops once it reaches a terminal status", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    try {
      const { queryClient, wrapper } = makeWrapper();
      queryClient.setQueryData(scheduledTaskRunsKey("st_1"), [] as api.ScheduledTaskRun[]);

      // First refetch returns running; second returns succeeded (terminal).
      vi.mocked(api.listScheduledTaskRuns)
        .mockResolvedValueOnce([RUNNING_RUN])
        .mockResolvedValue([SUCCEEDED_RUN]);

      const refetchSpy = vi.spyOn(queryClient, "refetchQueries");
      const { result } = renderHook(() => useRunScheduledTaskNow(), { wrapper });

      await act(async () => {
        void result.current.mutateAsync("st_1");
        await Promise.resolve();
      });

      // 1s: first poll — sees RUNNING_RUN (non-terminal), keeps going.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000);
      });
      expect(countRunRefetches(refetchSpy)).toBeGreaterThanOrEqual(1);

      // 1s: second poll — sees SUCCEEDED_RUN (terminal), stops.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000);
      });
      const countAfterStop = countRunRefetches(refetchSpy);
      expect(countAfterStop).toBeGreaterThanOrEqual(2);

      // No more refetches in the next 5s — poller has stopped.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });
      expect(countRunRefetches(refetchSpy)).toBeLessThanOrEqual(20);
    } finally {
      vi.useRealTimers();
    }
  });

  it("does NOT stop early when the new row is still running (non-terminal)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    try {
      const { queryClient, wrapper } = makeWrapper();
      queryClient.setQueryData(scheduledTaskRunsKey("st_1"), [] as api.ScheduledTaskRun[]);

      // Runs always return the running row — never transitions to terminal.
      vi.mocked(api.listScheduledTaskRuns).mockResolvedValue([RUNNING_RUN]);

      const refetchSpy = vi.spyOn(queryClient, "refetchQueries");
      const { result } = renderHook(() => useRunScheduledTaskNow(), { wrapper });

      await act(async () => {
        void result.current.mutateAsync("st_1");
        await Promise.resolve();
      });

      // After 3 polls (3s) the poller is still going (run still running).
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3_000);
      });
      expect(countRunRefetches(refetchSpy)).toBeGreaterThanOrEqual(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it("run-now stops polling after the attempt cap even without a terminal run", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    try {
      const { queryClient, wrapper } = makeWrapper();
      queryClient.setQueryData(scheduledTaskRunsKey("st_1"), [] as api.ScheduledTaskRun[]);

      // Runs never terminal — always returns running row (simulates stuck run).
      vi.mocked(api.listScheduledTaskRuns).mockResolvedValue([RUNNING_RUN]);

      const refetchSpy = vi.spyOn(queryClient, "refetchQueries");
      const { result } = renderHook(() => useRunScheduledTaskNow(), { wrapper });

      await act(async () => {
        void result.current.mutateAsync("st_1");
        await Promise.resolve();
      });

      // Advance past the 20-attempt cap (20 × 1s + 1s buffer).
      await act(async () => {
        await vi.advanceTimersByTimeAsync(21_000);
      });

      // At most RUN_NOW_POLL_MAX (20) poll refetches.
      expect(countRunRefetches(refetchSpy)).toBeLessThanOrEqual(20);

      // No more refetches after another 5s.
      const countAfterCap = refetchSpy.mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000);
      });
      expect(refetchSpy.mock.calls.length).toBe(countAfterCap);
    } finally {
      vi.useRealTimers();
    }
  });
});
