// Unit tests for `scheduledTasksApi.ts` — happy-path requests with a mocked
// `fetch`, snake↔camel conversion at the boundary, the optional-field omission
// rules on create/update, and the typed error path.
//
// Mirrors the `sessionsApi.test.ts` pattern: one fetchMock per test,
// vi.stubGlobal/unstubAllGlobals around it.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ScheduledTaskApiError,
  createScheduledTask,
  deleteScheduledTask,
  getScheduledTask,
  listScheduledTaskRuns,
  listScheduledTasks,
  updateScheduledTask,
} from "./scheduledTasksApi";

function mockResponse(body: unknown, init?: { ok?: boolean; status?: number }): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: "OK",
    json: async () => body,
  } as unknown as Response;
}

const TASK_WIRE = {
  id: "st_1",
  name: "Nightly triage",
  prompt: "Triage new issues",
  rrule: "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
  owner_user_id: "alice",
  agent_id: "ag_1",
  timezone: "America/New_York",
  created_at: 1_700_000_000,
  updated_at: 1_700_000_100,
  model_override: null,
  reasoning_effort: null,
  workspace: null,
  host_id: null,
  state: "active",
  last_run_at: null,
  last_run_conversation_id: null,
};

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("listScheduledTasks", () => {
  it("GETs /v1/scheduled-tasks and camelCases the rows", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ scheduled_tasks: [TASK_WIRE] }));
    const tasks = await listScheduledTasks();
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/scheduled-tasks");
    expect(tasks).toHaveLength(1);
    expect(tasks[0]).toMatchObject({
      id: "st_1",
      name: "Nightly triage",
      ownerUserId: "alice",
      agentId: "ag_1",
      timezone: "America/New_York",
      state: "active",
      hostId: null,
      workspace: null,
    });
  });

  it("returns [] when the server omits the key", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({}));
    await expect(listScheduledTasks()).resolves.toEqual([]);
  });
});

describe("getScheduledTask", () => {
  it("GETs /v1/scheduled-tasks/{id} and encodes the id", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse(TASK_WIRE));
    const task = await getScheduledTask("st 1");
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/scheduled-tasks/st%201");
    expect(task.id).toBe("st_1");
  });
});

describe("listScheduledTaskRuns", () => {
  it("GETs the runs subpath and camelCases run rows", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        runs: [
          {
            id: "run_1",
            scheduled_task_id: "st_1",
            status: "succeeded",
            scheduled_at: 1_700_000_000,
            conversation_id: "conv_1",
            fired_at: 1_700_000_005,
            finished_at: 1_700_000_050,
            error_code: null,
          },
        ],
      }),
    );
    const runs = await listScheduledTaskRuns("st_1");
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/scheduled-tasks/st_1/runs");
    expect(runs[0]).toMatchObject({
      id: "run_1",
      scheduledTaskId: "st_1",
      status: "succeeded",
      conversationId: "conv_1",
      firedAt: 1_700_000_005,
      finishedAt: 1_700_000_050,
      errorCode: null,
    });
  });
});

describe("createScheduledTask", () => {
  it("POSTs required fields and omits unset optionals", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse(TASK_WIRE));
    await createScheduledTask({
      name: "Nightly triage",
      prompt: "Triage new issues",
      rrule: "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
      agentId: "ag_1",
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/v1/scheduled-tasks");
    expect(init.method).toBe("POST");
    const body = JSON.parse(init.body);
    expect(body).toEqual({
      name: "Nightly triage",
      prompt: "Triage new issues",
      rrule: "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
      agent_id: "ag_1",
    });
    // Optional keys must be absent, not null.
    expect(body).not.toHaveProperty("host_id");
    expect(body).not.toHaveProperty("workspace");
    expect(body).not.toHaveProperty("timezone");
  });

  it("includes the host/workspace pair and timezone when provided", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse(TASK_WIRE));
    await createScheduledTask({
      name: "n",
      prompt: "p",
      rrule: "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
      agentId: "ag_1",
      timezone: "UTC",
      hostId: "host_1",
      workspace: "/home/me/repo",
    });
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.timezone).toBe("UTC");
    expect(body.host_id).toBe("host_1");
    expect(body.workspace).toBe("/home/me/repo");
  });
});

describe("updateScheduledTask", () => {
  it("PATCHes only the fields present in the input", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ ...TASK_WIRE, state: "paused" }));
    const updated = await updateScheduledTask("st_1", { state: "paused" });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/v1/scheduled-tasks/st_1");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body)).toEqual({ state: "paused" });
    expect(updated.state).toBe("paused");
  });
});

describe("deleteScheduledTask", () => {
  it("DELETEs /v1/scheduled-tasks/{id}", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ deleted: true, id: "st_1" }));
    await deleteScheduledTask("st_1");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/v1/scheduled-tasks/st_1");
    expect(init.method).toBe("DELETE");
  });
});

describe("error path", () => {
  it("throws a ScheduledTaskApiError carrying the server code + message", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        { error: { code: "invalid_input", message: "invalid rrule: fires only once" } },
        { ok: false, status: 400 },
      ),
    );
    await expect(
      createScheduledTask({ name: "n", prompt: "p", rrule: "bad", agentId: "ag_1" }),
    ).rejects.toMatchObject({
      name: "ScheduledTaskApiError",
      status: 400,
      code: "invalid_input",
      message: "invalid rrule: fires only once",
    });
  });

  it("falls back to the status line on a non-error-shaped body", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse("nope", { ok: false, status: 500 }));
    const err = await listScheduledTasks().catch((e) => e);
    expect(err).toBeInstanceOf(ScheduledTaskApiError);
    expect(err.status).toBe(500);
    expect(err.code).toBeNull();
  });
});
