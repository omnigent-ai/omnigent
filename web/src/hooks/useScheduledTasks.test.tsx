// Tests for the scheduled-tasks React-Query hooks: the list query shape and the
// invalidate-on-success contract of the create / update / delete mutations.
// The API client is mocked so we assert on hook wiring, not HTTP.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
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

beforeEach(() => {
  vi.mocked(api.listScheduledTasks).mockResolvedValue([TASK]);
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
    // …and the fired task's run history.
    expect(spy).toHaveBeenCalledWith({ queryKey: scheduledTaskRunsKey("st_1") });
  });
});
