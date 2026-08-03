// Tests for the scheduled-tasks React-Query hooks: the list query shape, the
// invalidate-on-success contract of the create / update / delete mutations,
// and the accelerated post-run-now run-history poll.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/lib/scheduledTasksApi";
import {
  cancelRunNowPoll,
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

// Run row running, conversation running (fresh fire, nothing settled yet).
const RUNNING_RUN: api.ScheduledTaskRun = {
  ...RUN,
  status: "running",
  conversationStatus: "running",
};
// Run row terminal but the CONVERSATION is still running — the intermediate
// state the old poll stopped on too early (BUG 1). The poll must keep going.
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

// Capture the run-now poll's own setTimeout delays (isolate from react-query's
// internal timers by filtering to the poll's interval band, ~0.7s–4s).
function pollDelays(spy: { mock: { calls: unknown[][] } }): number[] {
  return spy.mock.calls
    .map((args) => args[1])
    .filter((d): d is number => typeof d === "number" && d >= 700 && d <= 4_000);
}

describe("run-now fast-poll after 202", () => {
  it("keeps polling while the CONVERSATION is still running even though the run row is terminal, and stops once it settles (BUG 1)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    try {
      const { queryClient, wrapper } = makeWrapper();
      // Register a queryFn so the poll's refetchQueries actually calls the mocked
      // API (consuming the mock sequence) and writes each transition into the
      // cache that the stop condition reads back.
      queryClient.setQueryDefaults(scheduledTaskRunsKey("st_1"), {
        queryFn: () => api.listScheduledTaskRuns("st_1"),
      });
      queryClient.setQueryData(scheduledTaskRunsKey("st_1"), [] as api.ScheduledTaskRun[]);

      // Tick 1: run running. Tick 2: run terminal but conversation STILL running
      // (the intermediate state the old poll stopped on too early). Tick 3+:
      // conversation settled to idle — only now may the poll stop.
      vi.mocked(api.listScheduledTaskRuns)
        .mockResolvedValueOnce([RUNNING_RUN])
        .mockResolvedValueOnce([SUCCEEDED_CONV_RUNNING_RUN])
        .mockResolvedValue([SUCCEEDED_RUN]);

      const refetchSpy = vi.spyOn(queryClient, "refetchQueries");
      const { result } = renderHook(() => useRunScheduledTaskNow(), { wrapper });

      await act(async () => {
        void result.current.mutateAsync("st_1");
        await Promise.resolve();
      });

      // Tick 1 (~750ms): run running — keep going.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(800);
      });
      expect(countRunRefetches(refetchSpy)).toBe(1);

      // Tick 2 (~1275ms later): run terminal but conversation running — MUST NOT
      // stop (the old bug). Poll continues.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_300);
      });
      expect(countRunRefetches(refetchSpy)).toBe(2);

      // Tick 3 (~2168ms later): conversation now idle — poll stops.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_200);
      });
      const countAfterStop = countRunRefetches(refetchSpy);
      expect(countAfterStop).toBe(3);

      // No further refetches once settled, even well past the ceiling.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(20_000);
      });
      expect(countRunRefetches(refetchSpy)).toBe(countAfterStop);
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops immediately when a terminal run has NO conversation (host-less skip)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    try {
      const { queryClient, wrapper } = makeWrapper();
      queryClient.setQueryDefaults(scheduledTaskRunsKey("st_1"), {
        queryFn: () => api.listScheduledTaskRuns("st_1"),
      });
      queryClient.setQueryData(scheduledTaskRunsKey("st_1"), [] as api.ScheduledTaskRun[]);

      // First (and every) tick returns a terminal skipped run with no conversation
      // — there is nothing to wait for, so the poll stops after one tick.
      vi.mocked(api.listScheduledTaskRuns).mockResolvedValue([SKIPPED_NO_CONV_RUN]);

      const refetchSpy = vi.spyOn(queryClient, "refetchQueries");
      const { result } = renderHook(() => useRunScheduledTaskNow(), { wrapper });

      await act(async () => {
        void result.current.mutateAsync("st_1");
        await Promise.resolve();
      });

      await act(async () => {
        await vi.advanceTimersByTimeAsync(800);
      });
      expect(countRunRefetches(refetchSpy)).toBe(1);

      // Poll stopped — no more refetches over the next 20s.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(20_000);
      });
      expect(countRunRefetches(refetchSpy)).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("uses EXPONENTIAL BACKOFF for the poll interval (BUG 2)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    const setTimeoutSpy = vi.spyOn(globalThis, "setTimeout");
    try {
      const { queryClient, wrapper } = makeWrapper();
      queryClient.setQueryData(scheduledTaskRunsKey("st_1"), [] as api.ScheduledTaskRun[]);

      // Never settles — always running — so we observe several backoff intervals.
      vi.mocked(api.listScheduledTaskRuns).mockResolvedValue([RUNNING_RUN]);

      const { result } = renderHook(() => useRunScheduledTaskNow(), { wrapper });
      await act(async () => {
        void result.current.mutateAsync("st_1");
        await Promise.resolve();
      });

      // Let several ticks fire.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(15_000);
      });

      const delays = pollDelays(setTimeoutSpy);
      // At least three intervals observed, each strictly larger than the last
      // until the per-tick cap (~4s) is hit.
      expect(delays.length).toBeGreaterThanOrEqual(3);
      expect(delays[0]).toBeLessThan(delays[1]!);
      expect(delays[1]).toBeLessThan(delays[2]!);
      // First interval is the fast initial tick (< 1s), and none exceed the cap.
      expect(delays[0]).toBeLessThan(1_000);
      expect(Math.max(...delays)).toBeLessThanOrEqual(4_000);
    } finally {
      setTimeoutSpy.mockRestore();
      vi.useRealTimers();
    }
  });

  it("run-now stops polling after the bound even without a settled run", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    try {
      const { queryClient, wrapper } = makeWrapper();
      queryClient.setQueryData(scheduledTaskRunsKey("st_1"), [] as api.ScheduledTaskRun[]);

      // Runs never settle — always returns running row (simulates stuck run).
      vi.mocked(api.listScheduledTaskRuns).mockResolvedValue([RUNNING_RUN]);

      const refetchSpy = vi.spyOn(queryClient, "refetchQueries");
      const { result } = renderHook(() => useRunScheduledTaskNow(), { wrapper });

      await act(async () => {
        void result.current.mutateAsync("st_1");
        await Promise.resolve();
      });

      // Advance well past the ~20s time budget.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });

      // Bounded — far fewer than the old ~20 fixed-interval ticks thanks to backoff.
      const countAfterBound = countRunRefetches(refetchSpy);
      expect(countAfterBound).toBeLessThanOrEqual(12);

      // No more refetches after another 10s — poller has stopped.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000);
      });
      expect(countRunRefetches(refetchSpy)).toBe(countAfterBound);
    } finally {
      vi.useRealTimers();
    }
  });

  it("cancelRunNowPoll stops the poller (no further refetches after unmount cancel)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    try {
      const { queryClient, wrapper } = makeWrapper();
      queryClient.setQueryData(scheduledTaskRunsKey("st_1"), [] as api.ScheduledTaskRun[]);

      // Never settles — the poll would otherwise keep going until the bound.
      vi.mocked(api.listScheduledTaskRuns).mockResolvedValue([RUNNING_RUN]);

      const refetchSpy = vi.spyOn(queryClient, "refetchQueries");
      const { result } = renderHook(() => useRunScheduledTaskNow(), { wrapper });

      await act(async () => {
        void result.current.mutateAsync("st_1");
        await Promise.resolve();
      });

      // One poll happens, then the page "unmounts" and cancels the poller.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(800);
      });
      const countBeforeCancel = countRunRefetches(refetchSpy);
      expect(countBeforeCancel).toBeGreaterThanOrEqual(1);

      act(() => {
        cancelRunNowPoll("st_1");
      });

      // No further poll refetches once cancelled, even well past the bound window.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });
      expect(countRunRefetches(refetchSpy)).toBe(countBeforeCancel);
    } finally {
      vi.useRealTimers();
    }
  });
});
