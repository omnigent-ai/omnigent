// Unit tests for `scheduledTasksApi.ts` — happy-path requests with a mocked
// `fetch`, snake↔camel conversion at the boundary, the optional-field omission
// rules on create/update, and the typed error path.
//
// Mirrors the `sessionsApi.test.ts` pattern: one fetchMock per test,
// vi.stubGlobal/unstubAllGlobals around it.

import { setOmnigentHostConfig } from "./host";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ScheduledTaskApiError,
  cloneCreatedMessage,
  cloneFailedMessage,
  cloneScheduledTask,
  createScheduledTask,
  deleteScheduledTask,
  getScheduledTask,
  listScheduledTaskRunPullRequests,
  listScheduledTaskRuns,
  listScheduledTasks,
  listSharedScheduledTasks,
  listAutomationFailures,
  runScheduledTaskNow,
  reportLocalTimezone,
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
  execution_target: "connected_host",
  state: "active",
  last_run_at: null,
  last_run_conversation_id: null,
};

const fetchMock = vi.fn();
const analytics = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  analytics.mockReset();
  setOmnigentHostConfig({ analytics });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  setOmnigentHostConfig({});
});

describe("runScheduledTaskNow", () => {
  it.each([
    [{ triggered: true, id: "st_1", run_id: "run_1" }, "run_1"],
    [{ triggered: true, id: "st_1" }, null],
  ])("returns the run ID when the server provides one: %j", async (body, expected) => {
    fetchMock.mockResolvedValueOnce(mockResponse(body, { status: 202 }));
    expect(await runScheduledTaskNow("st_1")).toBe(expected);
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/scheduled-tasks/st_1/run",
      expect.objectContaining({ method: "POST" }),
    );
  });
});

describe("listScheduledTasks", () => {
  // Servers that predate task projects or shareable omit the keys; absent is shareable.
  it.each([
    ["absent", {}, null, true],
    ["present", { project_id: "proj_1", shareable: false }, "proj_1", false],
  ])(
    "GETs /v1/scheduled-tasks and camelCases the rows (project and shareable %s)",
    async (_, extra, projectId, shareable) => {
      fetchMock.mockResolvedValueOnce(
        mockResponse({ scheduled_tasks: [{ ...TASK_WIRE, ...extra }] }),
      );
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
        projectId,
        shareable,
      });
    },
  );

  it("returns [] when the server omits the key", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({}));
    await expect(listScheduledTasks()).resolves.toEqual([]);
  });

  // Servers that predate task MCP services omit the key.
  it.each([
    ["absent", {}, []],
    ["present", { mcp_services: ["github", "slack"] }, ["github", "slack"]],
  ])("maps a task's MCP services (%s)", async (_, extra, mcpServices) => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ scheduled_tasks: [{ ...TASK_WIRE, ...extra }] }),
    );
    const [task] = await listScheduledTasks();
    expect(task.mcpServices).toEqual(mcpServices);
  });
});

describe("getScheduledTask", () => {
  it.each([undefined, null, 2.5])("reads the saved cost limit (%s)", async (maxCostUsd) => {
    fetchMock.mockResolvedValueOnce(mockResponse({ ...TASK_WIRE, max_cost_usd: maxCostUsd }));
    expect((await getScheduledTask("st_1")).maxCostUsd).toBe(maxCostUsd ?? null);
  });

  it("GETs /v1/scheduled-tasks/{id} and encodes the id", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse(TASK_WIRE));
    const task = await getScheduledTask("st 1");
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/scheduled-tasks/st%201");
    expect(task.id).toBe("st_1");
    // Older servers return only the caller's own tasks.
    expect(task).toMatchObject({ viewerRole: "owner", ownerDisplayName: null });
  });

  it("names the owner of a task shared with the caller", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ ...TASK_WIRE, viewer_role: "viewer", owner_display_name: "Alice" }),
    );
    expect(await getScheduledTask("st_1")).toMatchObject({
      viewerRole: "viewer",
      ownerUserId: "alice",
      ownerDisplayName: "Alice",
    });
  });
});

describe("listSharedScheduledTasks", () => {
  it("requests bounded pages and keeps a cursor after a short page", async () => {
    const shared = { ...TASK_WIRE, viewer_role: "viewer", owner_display_name: "Alice" };
    fetchMock.mockResolvedValueOnce(mockResponse({ scheduled_tasks: [], next_cursor: "st_0" }));
    expect(await listSharedScheduledTasks(null)).toEqual({ tasks: [], nextCursor: "st_0" });
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/scheduled-tasks/shared?limit=50");

    fetchMock.mockResolvedValueOnce(mockResponse({ scheduled_tasks: [shared], next_cursor: null }));
    const page = await listSharedScheduledTasks("st_0");
    expect(fetchMock.mock.calls[1][0]).toBe("/v1/scheduled-tasks/shared?limit=50&after=st_0");
    expect(page.nextCursor).toBeNull();
    expect(page.tasks.map((task) => [task.id, task.viewerRole, task.ownerDisplayName])).toEqual([
      ["st_1", "viewer", "Alice"],
    ]);
  });

  it("throws the server's error", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        { error: { code: "forbidden", message: "Agents cannot list" } },
        { ok: false, status: 403 },
      ),
    );
    await expect(listSharedScheduledTasks(null)).rejects.toMatchObject({
      status: 403,
      code: "forbidden",
    });
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
      // An older server sends no failure detail or session read state.
      errorDetail: null,
      conversationUpdatedAt: null,
      conversationStatus: null,
      viewerLastSeen: null,
      viewerUnread: null,
      sessionCostUsd: null,
      conversationPendingApprovals: 0,
    });
  });

  it("maps a run's failure detail and session read state when the server sends them", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        runs: [
          {
            id: "run_1",
            scheduled_task_id: "st_1",
            status: "failed",
            scheduled_at: 1_700_000_000,
            conversation_id: "conv_1",
            fired_at: 1_700_000_005,
            finished_at: 1_700_000_050,
            error_code: "sandbox_registration_timeout",
            error_detail: "the sandbox did not register within 720 seconds",
            conversation_updated_at: 1_700_000_060,
            conversation_status: "idle",
            viewer_last_seen: 1_700_000_055,
            viewer_unread: false,
            session_cost_usd: 0,
            conversation_pending_approvals: 2,
          },
        ],
      }),
    );
    const [run] = await listScheduledTaskRuns("st_1");
    expect(run).toMatchObject({
      errorDetail: "the sandbox did not register within 720 seconds",
      conversationUpdatedAt: 1_700_000_060,
      conversationStatus: "idle",
      viewerLastSeen: 1_700_000_055,
      viewerUnread: false,
      sessionCostUsd: 0,
      conversationPendingApprovals: 2,
    });
  });
});

describe("createScheduledTask", () => {
  it.each([undefined, null, 2.5])("sends a cost limit only when set (%s)", async (maxCostUsd) => {
    fetchMock.mockResolvedValueOnce(mockResponse(TASK_WIRE));
    await createScheduledTask({
      name: "Nightly review",
      prompt: "Review new issues",
      rrule: "FREQ=DAILY",
      agentId: "ag_1",
      maxCostUsd,
    });
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    if (maxCostUsd == null) expect(body).not.toHaveProperty("max_cost_usd");
    else expect(body.max_cost_usd).toBe(maxCostUsd);
  });

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
    expect(new Headers(init.headers).get("x-omnigent-automation-template")).toBe("custom");
    expect(analytics).toHaveBeenLastCalledWith(
      expect.objectContaining({
        interactionKind: "create_scheduled_task",
        phase: "complete",
        status: "success",
        name: "custom",
      }),
    );
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

  it("attributes template creation without including prompt text in analytics", async () => {
    const templateId = "jira";
    fetchMock.mockResolvedValueOnce(mockResponse(TASK_WIRE));
    await createScheduledTask({
      name: "private name",
      prompt: "private prompt",
      rrule: "FREQ=DAILY",
      agentId: "agent",
      templateId,
    });
    const [, init] = fetchMock.mock.calls[0];
    expect(new Headers(init.headers).get("x-omnigent-automation-source")).toBe("ui");
    expect(new Headers(init.headers).get("x-omnigent-automation-template")).toBe(templateId);
    expect(JSON.parse(init.body)).not.toHaveProperty("template_id");
    expect(analytics).toHaveBeenLastCalledWith(
      expect.objectContaining({ phase: "complete", status: "success", name: templateId }),
    );
    expect(JSON.stringify(analytics.mock.calls)).not.toContain("private");
  });

  it("sends the browser tools mode header so the source copy keeps it", async () => {
    const win = window as unknown as { omnigentDesktop?: unknown };
    const before = win.omnigentDesktop;
    win.omnigentDesktop = { browserTabs: () => undefined };
    try {
      fetchMock.mockResolvedValueOnce(mockResponse(TASK_WIRE));
      await createScheduledTask({
        name: "n",
        prompt: "p",
        rrule: "FREQ=DAILY",
        agentId: "agent",
        sourceSessionId: "conv_1",
      });
      const [, init] = fetchMock.mock.calls[0];
      expect(new Headers(init.headers).get("X-Omnigent-Browser-Agent-Tools")).toBe("on");
    } finally {
      win.omnigentDesktop = before;
    }
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
      baseBranch: "HEAD",
    });
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.timezone).toBe("UTC");
    expect(body.host_id).toBe("host_1");
    expect(body.workspace).toBe("/home/me/repo");
    expect(body.base_branch).toBe("HEAD");
  });

  it("sends a project id and omits a null one", async () => {
    fetchMock.mockResolvedValue(mockResponse(TASK_WIRE));
    const base = { name: "n", prompt: "p", rrule: "FREQ=DAILY", agentId: "ag_1" };
    await createScheduledTask({ ...base, projectId: "proj_1" });
    await createScheduledTask({ ...base, projectId: null });
    const bodies = fetchMock.mock.calls.map(([, init]) => JSON.parse(init.body));
    expect(bodies[0].project_id).toBe("proj_1");
    expect(bodies[1]).not.toHaveProperty("project_id");
  });

  it("sends picked MCP services and omits an empty pick", async () => {
    fetchMock.mockResolvedValue(mockResponse(TASK_WIRE));
    const base = { name: "n", prompt: "p", rrule: "FREQ=DAILY", agentId: "ag_1" };
    await createScheduledTask({ ...base, mcpServices: ["github"] });
    await createScheduledTask({ ...base, mcpServices: [] });
    const bodies = fetchMock.mock.calls.map(([, init]) => JSON.parse(init.body));
    expect(bodies[0].mcp_services).toEqual(["github"]);
    expect(bodies[1]).not.toHaveProperty("mcp_services");
  });
});

describe("updateScheduledTask", () => {
  it.each([undefined, null, 2.5])(
    "preserves, clears, or sets the cost limit (%s)",
    async (maxCostUsd) => {
      fetchMock.mockResolvedValueOnce(mockResponse(TASK_WIRE));
      await updateScheduledTask("st_1", { maxCostUsd });
      expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(
        maxCostUsd === undefined ? {} : { max_cost_usd: maxCostUsd },
      );
    },
  );

  it("PATCHes only the fields present in the input", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ ...TASK_WIRE, state: "paused" }));
    const updated = await updateScheduledTask("st_1", { state: "paused" });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/v1/scheduled-tasks/st_1");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body)).toEqual({ state: "paused" });
    expect(updated.state).toBe("paused");
  });

  it.each([
    ["moves future runs to a project", "proj_2"],
    ["sends an explicit null to unfile future runs", null],
  ])("%s", async (_, projectId) => {
    fetchMock.mockResolvedValueOnce(mockResponse({ ...TASK_WIRE, project_id: projectId }));
    await updateScheduledTask("st_1", { projectId });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ project_id: projectId });
  });

  it.each([
    ["replaces the MCP services", ["slack"]],
    ["sends an explicit [] to turn them all off", []],
  ])("%s", async (_, mcpServices) => {
    fetchMock.mockResolvedValueOnce(mockResponse({ ...TASK_WIRE, mcp_services: mcpServices }));
    await updateScheduledTask("st_1", { mcpServices });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ mcp_services: mcpServices });
  });
});

describe("cloneScheduledTask", () => {
  it("POSTs /v1/scheduled-tasks/{id}/clone and returns the copy", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ ...TASK_WIRE, id: "st_2", name: "Nightly triage (copy)" }),
    );
    const clone = await cloneScheduledTask({ id: "st 1", hostId: "host_a" });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/v1/scheduled-tasks/st%201/clone");
    expect(init.method).toBe("POST");
    // The original's host routes the request to its replica.
    expect(JSON.parse(init.body)).toEqual({ host_id: "host_a" });
    expect(new Headers(init.headers).get("x-omnigent-automation-source")).toBe("ui");
    expect(clone).toMatchObject({ id: "st_2", name: "Nightly triage (copy)" });
  });

  it("sends the browser tools mode header so the copy's source session keeps it", async () => {
    const win = window as unknown as { omnigentDesktop?: unknown };
    const before = win.omnigentDesktop;
    win.omnigentDesktop = { browserTabs: () => undefined };
    try {
      fetchMock.mockResolvedValueOnce(mockResponse({ ...TASK_WIRE, id: "st_2" }));
      await cloneScheduledTask({ id: "st_1", hostId: null });
      const [, init] = fetchMock.mock.calls[0];
      // An unpinned automation names no host.
      expect(JSON.parse(init.body)).toEqual({});
      expect(new Headers(init.headers).get("X-Omnigent-Browser-Agent-Tools")).toBe("on");
    } finally {
      win.omnigentDesktop = before;
    }
  });

  it("throws the server's reason", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        { error: { code: "invalid_input", message: "Only an automation can be cloned" } },
        { ok: false, status: 400 },
      ),
    );
    const error = await cloneScheduledTask({ id: "st_1", hostId: null }).catch(
      (err: unknown) => err,
    );
    expect(error).toMatchObject({ status: 400, message: "Only an automation can be cloned" });
    expect(cloneFailedMessage(error)).toBe(
      "Couldn't clone the automation: Only an automation can be cloned",
    );
  });

  it.each([
    ["a server error", new ScheduledTaskApiError("boom", 500, null)],
    ["a network failure", new Error("offline")],
  ])("keeps the reason out of the toast for %s", (_label, error) => {
    expect(cloneFailedMessage(error)).toBe("Couldn't clone the automation");
  });

  it.each([
    [
      "an armed copy",
      { state: "active", nextRunAt: "2026-11-20T09:00:00Z" },
      "Automation cloned. Pause or delete the original so they don't both run.",
    ],
    [
      "a paused copy",
      { state: "paused", nextRunAt: null },
      "Automation cloned. The copy is paused like the original.",
    ],
    [
      "a copy whose schedule ran out",
      { state: "active", nextRunAt: null },
      "Automation cloned. Its schedule has no runs left.",
    ],
  ] as const)("tells the owner what's left after %s", (_label, clone, text) => {
    expect(cloneCreatedMessage(clone)).toBe(text);
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

describe("reportLocalTimezone", () => {
  const mockBrowserZone = (timeZone: string) =>
    vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockReturnValue({
      timeZone,
    } as Intl.ResolvedDateTimeFormatOptions);

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("PUTs the browser's zone as the caller's default", async () => {
    mockBrowserZone("America/Chicago");
    fetchMock.mockResolvedValueOnce(mockResponse({ timezone: "America/Chicago" }));
    await reportLocalTimezone();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/v1/scheduled-tasks/default-timezone");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body)).toEqual({ timezone: "America/Chicago" });
  });

  it.each(["empty", "throws"])("does not report a zone when discovery %s", async (failure) => {
    if (failure === "throws") {
      vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockImplementationOnce(() => {
        throw new Error("timezone discovery unavailable");
      });
    } else {
      mockBrowserZone("");
    }
    await expect(reportLocalTimezone()).resolves.toBeUndefined();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([
    ["a failed request", () => fetchMock.mockRejectedValueOnce(new TypeError("offline"))],
    [
      "a server that predates the endpoint",
      () => fetchMock.mockResolvedValueOnce(mockResponse({}, { ok: false, status: 405 })),
    ],
  ])("ignores %s", async (_case, respond) => {
    mockBrowserZone("America/Chicago");
    respond();
    await expect(reportLocalTimezone()).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("error path", () => {
  it("records a failed creation on network failure", async () => {
    fetchMock.mockRejectedValueOnce(new Error("offline"));
    await expect(
      createScheduledTask({ name: "n", prompt: "p", rrule: "bad", agentId: "agent" }),
    ).rejects.toThrow();
    expect(analytics).toHaveBeenLastCalledWith(
      expect.objectContaining({ phase: "complete", status: "failure" }),
    );
    expect(analytics.mock.calls).toHaveLength(2);
  });

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
    expect(analytics).toHaveBeenLastCalledWith(
      expect.objectContaining({ phase: "complete", status: "failure" }),
    );
    expect(analytics.mock.calls).toHaveLength(2);
  });

  it("falls back to the status line on a non-error-shaped body", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse("nope", { ok: false, status: 500 }));
    const err = await listScheduledTasks().catch((e) => e);
    expect(err).toBeInstanceOf(ScheduledTaskApiError);
    expect(err.status).toBe(500);
    expect(err.code).toBeNull();
  });
});

it("requests bounded automation Inbox pages and preserves empty continuations", async () => {
  fetchMock.mockResolvedValue(mockResponse({ data: [], next_cursor: "100:task" }));
  expect(await listAutomationFailures(null)).toEqual({ data: [], nextCursor: "100:task" });
  expect(String(fetchMock.mock.calls[0]![0])).toBe("/v1/scheduled-tasks/inbox?limit=50");
  fetchMock.mockResolvedValue(
    mockResponse({
      data: [
        {
          task_id: "task",
          task_name: "Report",
          run_id: "run",
          scheduled_at: 100,
          finished_at: null,
          error_code: "host_offline",
        },
      ],
      next_cursor: null,
    }),
  );
  expect(await listAutomationFailures("100:task")).toEqual({
    data: [
      {
        taskId: "task",
        taskName: "Report",
        runId: "run",
        scheduledAt: 100,
        finishedAt: null,
        errorCode: "host_offline",
      },
    ],
    nextCursor: null,
  });
  expect(String(fetchMock.mock.calls[1]![0])).toBe(
    "/v1/scheduled-tasks/inbox?limit=50&after=100%3Atask",
  );
});

describe("random worktree setting", () => {
  it.each([null, "HEAD"])("round trips an explicit %s update", async (baseBranch) => {
    fetchMock.mockResolvedValueOnce(mockResponse({ ...TASK_WIRE, base_branch: baseBranch }));
    const task = await updateScheduledTask("st_1", { baseBranch });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ base_branch: baseBranch });
    expect(task.baseBranch).toBe(baseBranch);
  });
});

describe("listScheduledTaskRunPullRequests", () => {
  it("pages after a run and maps each run's PRs and the refresh flag", async () => {
    const pr = {
      url: "https://github.com/example/repo/pull/1",
      provider: "github",
      repository: "example/repo",
      number: 1,
      relationship: "created",
      state: "MERGED",
      is_draft: false,
      state_checked_at: 1_700_000_100,
    };
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        runs: [{ run_id: "run_1", pull_requests: [pr] }, { run_id: "run_2" }],
        refresh_pending: true,
        next_cursor: null,
      }),
    );
    const page = await listScheduledTaskRunPullRequests("st/1", { limit: 20, after: "run 0" });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/scheduled-tasks/st%2F1/run-pull-requests?limit=20&after=run+0",
    );
    expect(page).toEqual({
      runs: [
        { runId: "run_1", pullRequests: [pr] },
        { runId: "run_2", pullRequests: [] },
      ],
      refreshPending: true,
    });
  });

  it("reads an answer without runs or the refresh flag as empty and settled", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({}));
    expect(await listScheduledTaskRunPullRequests("st_1", { limit: 20 })).toEqual({
      runs: [],
      refreshPending: false,
    });
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/scheduled-tasks/st_1/run-pull-requests?limit=20");
  });

  it("rejects with the server's error", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ detail: "Scheduled task not found" }, { ok: false, status: 404 }),
    );
    await expect(listScheduledTaskRunPullRequests("st_1", { limit: 20 })).rejects.toMatchObject({
      status: 404,
    });
  });
});
