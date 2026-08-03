// Tests for the scheduled-tasks React-Query hooks: the list query shape, the
// invalidate-on-success contract of the create / update / delete mutations, and
// the status-aware run-history refetchInterval that drives a run row to its
// terminal state live (replacing the old accelerated post-run-now poll).

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
  useScheduledTaskRuns,
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

  it("run-now calls the API and immediately invalidates the list, detail + runs", async () => {
    const { queryClient, wrapper } = makeWrapper();
    const spy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useRunScheduledTaskNow(), { wrapper });
    await result.current.mutateAsync("st_1");
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
  it("is true while a run row is still running or scheduled", () => {
    expect(hasUnsettledRun([RUNNING_RUN])).toBe(true);
    expect(hasUnsettledRun([{ ...RUN, status: "scheduled", conversationStatus: null }])).toBe(true);
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

      // …and polling STOPS: no further requests over a long quiet window.
      const callsAfterSettled = vi.mocked(api.listScheduledTaskRuns).mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000);
      });
      expect(vi.mocked(api.listScheduledTaskRuns).mock.calls.length).toBe(callsAfterSettled);
    } finally {
      vi.useRealTimers();
    }
  });

  it("never starts polling when the history is already all-terminal", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
    try {
      const { wrapper } = makeWrapper();
      vi.mocked(api.listScheduledTaskRuns).mockResolvedValue([SUCCEEDED_RUN]);

      const { result } = renderHook(() => useScheduledTaskRuns("st_1"), { wrapper });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(result.current.isSuccess).toBe(true);

      const callsAfterFirstFetch = vi.mocked(api.listScheduledTaskRuns).mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000);
      });
      expect(vi.mocked(api.listScheduledTaskRuns).mock.calls.length).toBe(callsAfterFirstFetch);
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
