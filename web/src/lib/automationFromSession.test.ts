import { createElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import { SCHEDULED_TASKS_KEY } from "@/hooks/useScheduledTasks";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";
import * as scheduledTasksApi from "@/lib/scheduledTasksApi";

vi.mock("@/lib/scheduledTasksApi", async (importOriginal) => ({
  ...(await importOriginal<typeof scheduledTasksApi>()),
  deleteScheduledTask: vi.fn(),
}));

import {
  automationPagePath,
  canCreateAutomationFromSession,
  SCHEDULED_TASK_SOURCE_LABEL_KEY,
  automationSourceTaskId,
  useDeleteSourceAutomations,
} from "./automationFromSession";

const INFO: ServerInfo = {
  ...FALLBACK_SERVER_INFO,
  automations_enabled: true,
  scheduled_task_source_sessions: true,
};

const SESSION: Conversation = {
  id: "conv_1",
  object: "conversation",
  title: "Weekly rubric",
  created_at: 1,
  updated_at: 1,
  labels: {},
  permission_level: null,
  agent_id: "ag_1",
};

describe("canCreateAutomationFromSession", () => {
  const allowed = { info: INFO, automationsEnabled: true, conversation: SESSION, isOwner: true };

  it("offers the item for an owned agent session when the server supports it", () => {
    expect(canCreateAutomationFromSession(allowed)).toBe(true);
  });

  it.each([
    ["automations are off", { automationsEnabled: false }],
    ["server info is loading", { info: "loading" as const }],
    [
      "the server can't copy sessions",
      { info: { ...INFO, scheduled_task_source_sessions: false } },
    ],
    ["the viewer doesn't own the session", { isOwner: false }],
    ["the session has no agent", { conversation: { ...SESSION, agent_id: undefined } }],
    ["the row is a client-only placeholder", { conversation: { ...SESSION, provisional: true } }],
    [
      "the session is a side chat",
      { conversation: { ...SESSION, labels: { "omnigent.side_chat": "1" } } },
    ],
    [
      "an automation already runs from the session",
      { conversation: { ...SESSION, labels: { [SCHEDULED_TASK_SOURCE_LABEL_KEY]: "st_1" } } },
    ],
  ])("hides it when %s", (_reason, overrides) => {
    expect(canCreateAutomationFromSession({ ...allowed, ...overrides })).toBe(false);
  });
});

describe("automationSourceTaskId", () => {
  it("reads the automation id from the session's automation label", () => {
    expect(automationSourceTaskId({ labels: { [SCHEDULED_TASK_SOURCE_LABEL_KEY]: "st_1" } })).toBe(
      "st_1",
    );
    expect(automationSourceTaskId({ labels: {} })).toBeNull();
    expect(automationSourceTaskId(null)).toBeNull();
  });
});

describe("automationPagePath", () => {
  const task = { id: "st_1" } as scheduledTasksApi.ScheduledTask;

  it("opens the new automation's page when detail pages exist", () => {
    expect(automationPagePath({ ...INFO, features: { automations_v2: true } }, task)).toBe(
      "/tasks/st_1",
    );
  });

  it("falls back to the automation list", () => {
    expect(automationPagePath(INFO, task)).toBe("/tasks");
    expect(automationPagePath("loading", task)).toBe("/tasks");
  });
});

describe("useDeleteSourceAutomations", () => {
  const deleteScheduledTask = vi.mocked(scheduledTasksApi.deleteScheduledTask);
  let queryClient: QueryClient;

  beforeEach(() => {
    deleteScheduledTask.mockReset();
    queryClient = new QueryClient();
    queryClient.setQueryData(SCHEDULED_TASKS_KEY, [{ id: "st_1" }, { id: "st_2" }, { id: "st_3" }]);
  });

  function renderDelete() {
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    return renderHook(() => useDeleteSourceAutomations(), { wrapper }).result.current;
  }

  it("deletes each automation and drops it from the cached list, counting a 404 as deleted", async () => {
    deleteScheduledTask
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new scheduledTasksApi.ScheduledTaskApiError("gone", 404, null));

    await expect(renderDelete()(["st_1", "st_2"])).resolves.toBe(true);

    expect(deleteScheduledTask).toHaveBeenCalledWith("st_1");
    expect(deleteScheduledTask).toHaveBeenCalledWith("st_2");
    expect(queryClient.getQueryData(SCHEDULED_TASKS_KEY)).toEqual([{ id: "st_3" }]);
  });

  it("reports a failed delete so the caller keeps the sessions", async () => {
    deleteScheduledTask
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new scheduledTasksApi.ScheduledTaskApiError("boom", 500, null));

    await expect(renderDelete()(["st_1", "st_2"])).resolves.toBe(false);

    expect(queryClient.getQueryData(SCHEDULED_TASKS_KEY)).toEqual([{ id: "st_2" }, { id: "st_3" }]);
  });
});
