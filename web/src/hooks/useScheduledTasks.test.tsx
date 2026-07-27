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

describe("run-now fast-poll after 202", () => {
  it("starts polling runs after run-now and stops once a new row appears", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });

    const { queryClient, wrapper } = makeWrapper();

    // Seed the runs query directly so there is no async waitFor needed.
    queryClient.setQueryData(scheduledTaskRunsKey("st_1"), [] as api.ScheduledTaskRun[]);

    // First refetch still returns empty; subsequent return [RUN] (new row appeared).
    vi.mocked(api.listScheduledTaskRuns).mockResolvedValueOnce([]).mockResolvedValue([RUN]);

    const refetchSpy = vi.spyOn(queryClient, "refetchQueries");

    const { result } = renderHook(() => useRunScheduledTaskNow(), { wrapper });

    // Fire the mutation (202 response resolves, onSuccess runs synchronously in fake-timer mode).
    await act(async () => {
      void result.current.mutateAsync("st_1");
      // Drain microtasks so onSuccess fires.
      await Promise.resolve();
    });

    // Advance 1s → first poll fires and resolves.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });

    // Advance 1s → second poll fires (run now visible, poller should stop after this).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });

    // At least 2 refetches of the runs query (poll started and ran).
    const runRefetches = refetchSpy.mock.calls.filter(
      (args) =>
        JSON.stringify(args[0]) === JSON.stringify({ queryKey: scheduledTaskRunsKey("st_1") }),
    );
    expect(runRefetches.length).toBeGreaterThanOrEqual(2);

    // The poll is bounded: even after a further 5s, total refetches stay <= the cap.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    const totalRunRefetches = refetchSpy.mock.calls.filter(
      (args) =>
        JSON.stringify(args[0]) === JSON.stringify({ queryKey: scheduledTaskRunsKey("st_1") }),
    );
    expect(totalRunRefetches.length).toBeLessThanOrEqual(10);

    vi.useRealTimers();
  });

  it("run-now stops polling after the attempt cap even without a new row", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });

    const { queryClient, wrapper } = makeWrapper();

    // Seed empty runs directly — no async fetch needed.
    queryClient.setQueryData(scheduledTaskRunsKey("st_1"), [] as api.ScheduledTaskRun[]);

    // Runs never appear — always empty.
    vi.mocked(api.listScheduledTaskRuns).mockResolvedValue([]);

    const refetchSpy = vi.spyOn(queryClient, "refetchQueries");

    const { result } = renderHook(() => useRunScheduledTaskNow(), { wrapper });

    await act(async () => {
      void result.current.mutateAsync("st_1");
      await Promise.resolve();
    });

    // Advance past the 10-attempt cap (10 × 1s + 1s buffer).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(11_000);
    });

    const runRefetches = refetchSpy.mock.calls.filter(
      (args) =>
        JSON.stringify(args[0]) === JSON.stringify({ queryKey: scheduledTaskRunsKey("st_1") }),
    );
    // At most _RUN_NOW_POLL_MAX (10) poll refetches.
    expect(runRefetches.length).toBeLessThanOrEqual(10);

    // No more refetches after another 5s.
    const countAfterCap = refetchSpy.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(refetchSpy.mock.calls.length).toBe(countAfterCap);

    vi.useRealTimers();
  });
});
