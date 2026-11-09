import { createElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
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
  automationDeletedWith,
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

  it("deletes it with the session unless automations are off for the caller", () => {
    const session = { labels: { [SCHEDULED_TASK_SOURCE_LABEL_KEY]: "st_1" } };
    expect(automationDeletedWith(INFO, session)).toBe("st_1");
    expect(automationDeletedWith("loading", session)).toBe("st_1");
    expect(automationDeletedWith({ ...INFO, automations_enabled: false }, session)).toBeNull();
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

  function renderDelete(path = "/") {
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(
        QueryClientProvider,
        { client: queryClient },
        createElement(MemoryRouter, { initialEntries: [path] }, children),
      );
    return renderHook(
      () => ({
        remove: useDeleteSourceAutomations(),
        location: useLocation(),
        navigate: useNavigate(),
      }),
      { wrapper },
    ).result;
  }

  it("deletes each automation and drops it from the cached list, counting not_found as deleted", async () => {
    deleteScheduledTask
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new scheduledTasksApi.ScheduledTaskApiError("gone", 404, "not_found"));

    await expect(renderDelete().current.remove(["st_1", "st_2"], [])).resolves.toBe(true);

    expect(deleteScheduledTask).toHaveBeenCalledWith("st_1");
    expect(deleteScheduledTask).toHaveBeenCalledWith("st_2");
    expect(queryClient.getQueryData(SCHEDULED_TASKS_KEY)).toEqual([{ id: "st_3" }]);
  });

  it.each([
    ["a server error", new scheduledTasksApi.ScheduledTaskApiError("boom", 500, null)],
    // Automations turned off for the caller: the gate's 404 deleted nothing.
    ["the automations gate", new scheduledTasksApi.ScheduledTaskApiError("not found", 404, null)],
  ])("reports a failed delete (%s) so the caller keeps the sessions", async (_reason, error) => {
    deleteScheduledTask.mockResolvedValueOnce(undefined).mockRejectedValueOnce(error);
    queryClient.setQueryData(["session", "conv_1"], { id: "conv_1", labels: SESSION.labels });
    const result = renderDelete("/c/conv_1");

    await act(() =>
      expect(result.current.remove(["st_1", "st_2"], ["conv_1"])).resolves.toBe(false),
    );

    expect(queryClient.getQueryData(SCHEDULED_TASKS_KEY)).toEqual([{ id: "st_2" }, { id: "st_3" }]);
    // The kept session stays open, and its own entry refetches the label it lost.
    expect(result.current.location.pathname).toBe("/c/conv_1");
    expect(queryClient.getQueryState(["session", "conv_1"])?.isInvalidated).toBe(true);
  });

  it.each<[string, string | null, string]>([
    ["/tasks/st_1", null, "/tasks"],
    ["/tasks/st_3", null, "/tasks/st_3"],
    ["/c/conv_1", null, "/"],
    ["/c/conv_2", null, "/c/conv_2"],
    // The page the user is on once the delete finishes decides.
    ["/tasks/st_1", "/c/conv_2", "/c/conv_2"],
    ["/c/conv_2", "/tasks/st_1", "/tasks"],
    ["/c/conv_1", "/c/conv_2", "/c/conv_2"],
    ["/c/conv_2", "/c/conv_1", "/"],
  ])(
    "deleting conv_1's automation from %s, then %s mid-delete, ends on %s",
    async (from, moved, to) => {
      let finish = () => {};
      deleteScheduledTask.mockReturnValue(
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
      );
      const result = renderDelete(from);

      let removal = Promise.resolve(true);
      act(() => {
        removal = result.current.remove(["st_1"], ["conv_1"]);
      });
      if (moved !== null) act(() => result.current.navigate(moved));
      await act(async () => {
        finish();
        await removal;
      });

      expect(result.current.location.pathname).toBe(to);
    },
  );
});
