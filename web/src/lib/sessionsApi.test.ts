// Unit tests for `sessionsApi.ts` — happy-path POSTs with mocked
// `fetch`, plus argument-shape pins for `interrupt` and `approve`.
//
// These tests primarily guard the camelCase TS ↔ snake_case wire
// boundary: a regression here would mean the store hits an endpoint
// with the wrong field names, which the server would 422 with no
// useful client-side error trail.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  apiErrorFromResponse,
  approve,
  bindOnlyOnlineRunner,
  createBundledSession,
  createSession,
  createSideChat,
  exportSessionTranscript,
  fetchSessionItemsPage,
  forkSession,
  getSession,
  getSessionSlim,
  getSessionUsage,
  importCodeIsRetryable,
  importErrorFromException,
  importLocalSessions,
  interrupt,
  listRunners,
  openSessionStream,
  postEvent,
  continueFailedTurn,
  SESSION_HISTORY_PAGE_SIZE,
  stopSession,
  updateSession,
  validationDetailMessage,
} from "./sessionsApi";
import { BACKGROUND_SESSION_TITLES_STORAGE_KEY } from "./backgroundSessionTitlesPreferences";
import { getSessionHost, setSessionHost } from "./sessionHost";

function mockJsonResponse(
  body: unknown,
  init?: { ok?: boolean; status?: number; statusText?: string },
): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: init?.statusText ?? "OK",
    json: async () => body,
  } as unknown as Response;
}

// An NDJSON streaming response: each line is emitted as its own chunk so the
// reader sees them arrive one at a time, matching the `/imports/local` stream.
function mockNdjsonResponse(lines: string[]): Response {
  const encoder = new TextEncoder();
  let i = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i < lines.length) {
        controller.enqueue(encoder.encode(lines[i] + "\n"));
        i += 1;
      } else {
        controller.close();
      }
    },
  });
  return { ok: true, status: 200, statusText: "OK", body } as unknown as Response;
}

// An NDJSON body whose reader rejects (fetch's mid-body "network error"
// TypeError) after the given lines, or — with `rejectWith: null` — closes
// mid-line, as a proxy cutting the connection would leave it.
function mockBrokenNdjsonResponse(
  lines: string[],
  { partialTail = "", rejectWith = new TypeError("network error") as Error | null } = {},
): Response {
  const encoder = new TextEncoder();
  let i = 0;
  let tail = partialTail;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i < lines.length) {
        controller.enqueue(encoder.encode(lines[i] + "\n"));
        i += 1;
      } else if (tail) {
        controller.enqueue(encoder.encode(tail));
        tail = "";
      } else if (rejectWith !== null) {
        controller.error(rejectWith);
      } else {
        controller.close();
      }
    },
  });
  return { ok: true, status: 200, statusText: "OK", body } as unknown as Response;
}

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("apiErrorFromResponse", () => {
  it("reads the AP `error` envelope (message + code)", async () => {
    const err = await apiErrorFromResponse(
      mockJsonResponse(
        { error: { code: "conflict", message: "Session is busy." } },
        { ok: false, status: 409 },
      ),
    );
    expect(err.message).toBe("Session is busy.");
    expect(err.code).toBe("conflict");
    expect(err.status).toBe(409);
  });

  it("reads a top-level error envelope (error_code + message), as storage backends send", async () => {
    const err = await apiErrorFromResponse(
      mockJsonResponse(
        {
          error_code: "INVALID_PARAMETER_VALUE",
          message: "Workspace items cannot contain the '/' character",
        },
        { ok: false, status: 400 },
      ),
    );
    expect(err.message).toBe("Workspace items cannot contain the '/' character");
    expect(err.code).toBe("INVALID_PARAMETER_VALUE");
    expect(err.status).toBe(400);
  });

  it("reads a FastAPI 422 validation list as its first readable message", async () => {
    const err = await apiErrorFromResponse(
      mockJsonResponse(
        {
          detail: [
            { type: "missing", loc: ["body", "host_id"], msg: "Field required" },
            { type: "int_parsing", loc: ["body", "limit"], msg: "Input should be an integer" },
          ],
        },
        { ok: false, status: 422, statusText: "Unprocessable Entity" },
      ),
    );
    expect(err.message).toBe("host_id: Field required");
    expect(err.code).toBeNull();
    expect(err.status).toBe(422);
  });

  it("keeps the AP error envelope ahead of a 422 detail list", async () => {
    const err = await apiErrorFromResponse(
      mockJsonResponse(
        { error: { code: "invalid_input", message: "Too many items." }, detail: [{ msg: "x" }] },
        { ok: false, status: 422 },
      ),
    );
    expect(err.message).toBe("Too many items.");
  });

  it("falls back to the status line for a 422 list with no readable message", async () => {
    const err = await apiErrorFromResponse(
      mockJsonResponse(
        { detail: [{ loc: ["body"] }, null, { msg: "  " }] },
        { ok: false, status: 422, statusText: "Unprocessable Entity" },
      ),
    );
    expect(err.message).toBe("422 Unprocessable Entity");
  });

  it("exposes the remaining error-envelope fields as details", async () => {
    const err = await apiErrorFromResponse(
      mockJsonResponse(
        {
          error: {
            code: "conflict",
            message: "Already imported.",
            import_code: "already_imported",
            retryable: false,
            session_id: "conv_1",
          },
        },
        { ok: false, status: 409 },
      ),
    );
    expect(err.importCode).toBe("already_imported");
    expect(err.retryable).toBe(false);
    expect(err.details).toEqual({ session_id: "conv_1" });
  });

  it("leaves import fields null for an error without them", async () => {
    const err = await apiErrorFromResponse(
      mockJsonResponse(
        { error: { code: "conflict", message: "Busy." } },
        { ok: false, status: 409 },
      ),
    );
    expect(err.importCode).toBeNull();
    expect(err.retryable).toBeNull();
    expect(err.details).toEqual({});
  });

  it("falls back to the status line when the body is not an error shape", async () => {
    const err = await apiErrorFromResponse(
      mockJsonResponse({}, { ok: false, status: 404, statusText: "Not Found" }),
    );
    expect(err.message).toBe("404 Not Found");
    expect(err.code).toBeNull();
  });
});

describe("createSession", () => {
  it("POSTs agent_id (snake_case) and parses the snake_case response", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "agent_xyz",
        status: "idle",
        created_at: 1704067200,
        items: [],
      }),
    );

    const session = await createSession("agent_xyz");

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions");
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    expect(JSON.parse(init.body as string)).toEqual({
      agent_id: "agent_xyz",
      initial_items: [],
    });
    expect(session).toEqual({
      id: "conv_abc",
      agentId: "agent_xyz",
      agentName: null,
      runnerId: undefined,
      hostId: null,
      hostResumable: false,
      archived: false,
      status: "idle",
      createdAt: 1704067200,
      title: null,
      items: [],
      queuedItems: undefined,
      contextWindow: undefined,
      labels: undefined,
      lastTaskError: undefined,
      lastTotalTokens: undefined,
      usageIncluded: true,
      totalCostUsd: undefined,
      usageByModel: null,
      llmModel: undefined,
      harness: null,
      modelOverride: undefined,
      costControlModeOverride: undefined,
      shareWorkspaceFiles: false,
      reasoningEffort: undefined,
      pendingElicitations: [],
      pendingInputs: [],
      permissionLevel: null,
      parentSessionId: null,
      subAgentName: null,
      terminalLaunchArgs: null,
      kind: "default",
      backgroundTaskCount: undefined,
      todos: [],
      codexModelOptions: [],
      terminalPending: false,
      sandboxStatus: null,
      mcpStartup: null,
      activeResponseId: null,
      workspace: null,
      gitBranch: null,
    });
  });

  it("preserves the saved inference policy on an empty session catalog", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_policy",
        agent_id: "agent_xyz",
        status: "idle",
        created_at: 1704067200,
        inference_configured: true,
        inference_error: "Gateway unavailable",
        model_options: [],
      }),
    );
    const session = await createSession("agent_xyz");
    expect(session.inferenceConfigured).toBe(true);
    expect(session.inferenceError).toBe("Gateway unavailable");
    expect(session.codexModelOptions).toEqual([]);
  });

  it("forwards initial_items when provided", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "agent_xyz",
        status: "running",
        created_at: 1704067200,
      }),
    );
    const seed = [
      {
        type: "message",
        data: { role: "user", content: [{ type: "input_text", text: "hi" }] },
      },
    ];

    await createSession("agent_xyz", seed);

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string).initial_items).toEqual(seed);
  });

  it("sends the local opt-out header when background titles are disabled", async () => {
    localStorage.setItem(BACKGROUND_SESSION_TITLES_STORAGE_KEY, "off");
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "agent_xyz",
        status: "idle",
        created_at: 1704067200,
      }),
    );

    await createSession("agent_xyz");

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("X-Omnigent-Background-Session-Titles")).toBe("off");
  });

  it("forwards parent_session_id, sub_agent_name and title for the Add-agent path", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_child",
        agent_id: "agent_xyz",
        status: "idle",
        created_at: 1704067200,
        parent_session_id: "conv_parent",
      }),
    );

    await createSession("agent_xyz", [], {
      parentSessionId: "conv_parent",
      subAgentName: null,
      title: "ui:claude-native-ui:1",
    });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    // Whole body asserted: proves the snake_case mapping AND that
    // sub_agent_name=null is sent verbatim (so the runner resolves the
    // child's own agent_id instead of a parent sub-spec).
    expect(JSON.parse(init.body as string)).toEqual({
      agent_id: "agent_xyz",
      initial_items: [],
      parent_session_id: "conv_parent",
      sub_agent_name: null,
      title: "ui:claude-native-ui:1",
    });
  });

  it("omits the optional fields entirely when no options are passed", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "agent_xyz",
        status: "idle",
        created_at: 1704067200,
      }),
    );

    await createSession("agent_xyz");

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const sent = JSON.parse(init.body as string);
    // Optional keys must be absent (not null/undefined) so the server
    // applies its own defaults — guards against always-sending them.
    expect("parent_session_id" in sent).toBe(false);
    expect("sub_agent_name" in sent).toBe(false);
    expect("title" in sent).toBe(false);
  });

  it("throws when the response is not ok", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({}, { ok: false, status: 404 }));
    await expect(createSession("missing")).rejects.toThrow(/404/);
  });

  it("forward-compat: reads queued_items from the snapshot when present", async () => {
    const queued = [{ type: "message", data: { role: "user", content: [] } }];
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "agent_xyz",
        status: "running",
        created_at: 1704067200,
        items: [],
        queued_items: queued,
      }),
    );

    const session = await createSession("agent_xyz");
    expect(session.queuedItems).toEqual(queued);
  });

  it("maps pending_inputs (snake) to pendingInputs (camel) with content", async () => {
    // The snapshot replays un-consumed native web messages here so the
    // store re-hydrates the optimistic bubble on rebind. Each entry's
    // pending_id becomes the bubble's stable key and the content is
    // carried through verbatim for rendering.
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "agent_xyz",
        status: "running",
        created_at: 1704067200,
        items: [],
        pending_inputs: [
          { pending_id: "pending_1", content: [{ type: "input_text", text: "queued" }] },
        ],
      }),
    );

    const session = await createSession("agent_xyz");
    expect(session.pendingInputs).toEqual([
      { pendingId: "pending_1", content: [{ type: "input_text", text: "queued" }] },
    ]);
  });

  it("maps active_response_id (snake) to activeResponseId (camel)", async () => {
    // The in-flight turn id lets a mid-turn reconnect reopen a streaming
    // activeResponse so native Claude's tool cards keep rendering live.
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "agent_xyz",
        status: "running",
        created_at: 1704067200,
        items: [],
        active_response_id: "resp_turn_1",
      }),
    );

    const session = await createSession("agent_xyz");
    expect(session.activeResponseId).toBe("resp_turn_1");
  });

  it("defaults activeResponseId to null when the snapshot omits it", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "agent_xyz",
        status: "idle",
        created_at: 1704067200,
        items: [],
      }),
    );

    const session = await createSession("agent_xyz");
    expect(session.activeResponseId).toBeNull();
  });
});

describe("createBundledSession", () => {
  it("sends the local opt-out header when background titles are disabled", async () => {
    localStorage.setItem(BACKGROUND_SESSION_TITLES_STORAGE_KEY, "off");
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        session_id: "conv_bundle",
      }),
    );

    const result = await createBundledSession(
      new File([], "agent.tar.gz", { type: "application/gzip" }),
      { workspace: "/tmp/project" },
    );

    expect(result.id).toBe("conv_bundle");
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("X-Omnigent-Background-Session-Titles")).toBe("off");
  });
});

describe("forkSession", () => {
  it("POSTs the fork endpoint with the (url-encoded) source id and parses the fork", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_fork",
        agent_id: "agent_clone",
        status: "idle",
        created_at: 1704067200,
        title: "Fork of My session",
        items: [],
      }),
    );

    const session = await forkSession("conv abc");

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv%20abc/fork");
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    // No title given → empty body so the server derives "Fork of <title>".
    expect(JSON.parse(init.body as string)).toEqual({});
    expect(session.id).toBe("conv_fork");
    expect(session.title).toBe("Fork of My session");
    expect(session.status).toBe("idle");
  });

  it("forwards the title when provided", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_fork",
        agent_id: "agent_clone",
        status: "idle",
        created_at: 1704067200,
      }),
    );

    await forkSession("conv_src", { title: "My clone" });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ title: "My clone" });
  });

  it("forwards run-config overrides (model / effort / launch args)", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_fork",
        agent_id: "agent_clone",
        status: "idle",
        created_at: 1704067200,
      }),
    );

    await forkSession("conv_src", {
      config: {
        modelOverride: "opus",
        reasoningEffort: "high",
        terminalLaunchArgs: ["--permission-mode", "auto"],
      },
    });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      model_override: "opus",
      reasoning_effort: "high",
      terminal_launch_args: ["--permission-mode", "auto"],
    });
  });

  it("omits run-config fields left undefined so the fork inherits them", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_fork",
        agent_id: "agent_clone",
        status: "idle",
        created_at: 1704067200,
      }),
    );

    // An empty config object (non-native target) sends no run overrides.
    await forkSession("conv_src", { config: {} });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({});
  });

  it("asks for a managed sandbox when a sandbox target is given", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_fork",
        agent_id: "agent_clone",
        status: "idle",
        created_at: 1704067200,
      }),
    );

    await forkSession("conv_src", {
      sandbox: { provider: "modal", workspace: "https://github.com/org/repo#main" },
    });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      host_type: "managed",
      sandbox_provider: "modal",
      workspace: "https://github.com/org/repo#main",
    });
  });

  it("keeps an explicit null workspace, so a sandbox fork can start empty", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_fork",
        agent_id: "agent_clone",
        status: "idle",
        created_at: 1704067200,
      }),
    );

    // Null is a real choice (empty sandbox); dropping the key would instead
    // inherit the source's repository server-side. A provider the server
    // didn't name is omitted so it picks its first.
    await forkSession("conv_src", { sandbox: { provider: null, workspace: null } });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ host_type: "managed", workspace: null });
  });

  it("sends no host_type when no sandbox target is given (the fork stays unbound)", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_fork",
        agent_id: "agent_clone",
        status: "idle",
        created_at: 1704067200,
      }),
    );

    await forkSession("conv_src", { config: {} });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).not.toHaveProperty("host_type");
  });

  it("surfaces a non-ok response as a thrown error (e.g. 403 no access)", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({}, { ok: false, status: 403 }));
    await expect(forkSession("conv_src")).rejects.toThrow(/403/);
  });
});

describe("createSideChat", () => {
  const source = {
    id: "conv_source",
    agent_id: "agent_source",
    status: "idle",
    created_at: 1704067200,
    host_id: "host_mac",
    workspace: "/Users/alice/project",
    runner_id: "runner_source",
    runner_online: true,
    host_online: true,
  };
  const fork = {
    id: "conv_side",
    agent_id: "agent_fork",
    status: "idle",
    created_at: 1704067200,
  };

  it.each(["worktree-from-another-machine", "main", null])(
    "uses the current workspace without requiring saved branch %s",
    async (gitBranch) => {
      fetchMock
        .mockResolvedValueOnce(mockJsonResponse({ ...source, git_branch: gitBranch }))
        .mockResolvedValueOnce(mockJsonResponse(fork))
        .mockResolvedValueOnce(mockJsonResponse({ runner_id: "runner_side" }));

      await expect(createSideChat(source.id)).resolves.toEqual({ childSessionId: fork.id });

      const [forkUrl, forkInit] = fetchMock.mock.calls[1] as [string, RequestInit];
      expect(forkUrl).toBe(`/v1/sessions/${source.id}/fork`);
      expect(JSON.parse(forkInit.body as string)).toEqual({ title: "Side chat", side_chat: true });
      const [launchUrl, launchInit] = fetchMock.mock.calls[2] as [string, RequestInit];
      expect(launchUrl).toBe("/v1/hosts/host_mac/runners");
      expect(JSON.parse(launchInit.body as string)).toEqual({
        session_id: fork.id,
        workspace: source.workspace,
      });
      expect(fetchMock).toHaveBeenCalledTimes(3);
    },
  );

  it.each([{ host_id: null, workspace: null }, { host_online: false }])(
    "uses the parent's online runner when its host cannot launch (%j)",
    async (placement) => {
      fetchMock
        .mockResolvedValueOnce(mockJsonResponse({ ...source, ...placement }))
        .mockResolvedValueOnce(mockJsonResponse(fork))
        .mockResolvedValueOnce(mockJsonResponse({ ...fork, runner_id: source.runner_id }));

      await expect(createSideChat(source.id)).resolves.toEqual({ childSessionId: fork.id });

      const [url, init] = fetchMock.mock.calls[2] as [string, RequestInit];
      expect(url).toBe(`/v1/sessions/${fork.id}`);
      expect(init.method).toBe("PATCH");
      expect(JSON.parse(init.body as string)).toEqual({ runner_id: source.runner_id });
      expect(fetchMock).toHaveBeenCalledTimes(3);
    },
  );

  it("allows an in-process session to use normal dispatch without a host or runner id", async () => {
    fetchMock
      .mockResolvedValueOnce(
        mockJsonResponse({ ...source, host_id: null, workspace: null, runner_id: null }),
      )
      .mockResolvedValueOnce(mockJsonResponse(fork));

    await expect(createSideChat(source.id)).resolves.toEqual({ childSessionId: fork.id });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("wakes a resumable host and refreshes its placement before starting a side chat", async () => {
    fetchMock
      .mockResolvedValueOnce(
        mockJsonResponse({
          ...source,
          host_resumable: true,
          host_online: false,
          runner_online: false,
        }),
      )
      .mockResolvedValueOnce(mockJsonResponse({ recovered: true, recovery: "runner_relaunched" }))
      .mockResolvedValueOnce(
        mockJsonResponse({ ...source, host_id: "host_awake", workspace: "/resumed/workspace" }),
      )
      .mockResolvedValueOnce(mockJsonResponse(fork))
      .mockResolvedValueOnce(mockJsonResponse({ runner_id: "runner_side" }));

    await expect(createSideChat(source.id)).resolves.toEqual({ childSessionId: fork.id });

    const [retryUrl, retryInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(retryUrl).toBe(`/v1/sessions/${source.id}/events`);
    expect(JSON.parse(retryInit.body as string)).toMatchObject({ type: "retry_session" });
    const [launchUrl, launchInit] = fetchMock.mock.calls[4] as [string, RequestInit];
    expect(launchUrl).toBe("/v1/hosts/host_awake/runners");
    expect(JSON.parse(launchInit.body as string)).toEqual({
      session_id: fork.id,
      workspace: "/resumed/workspace",
    });
  });

  it("does not create an orphan fork when neither the host nor runner is available", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({ ...source, host_online: false, runner_online: false }),
    );

    await expect(createSideChat(source.id)).rejects.toThrow("This session is disconnected.");
    expect(fetchMock).toHaveBeenCalledOnce();
  });
});

describe("runner binding", () => {
  it("lists online runners and parses harnesses", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        data: [
          {
            runner_id: "runner_abc",
            online: true,
            harnesses: ["openai-agents"],
          },
        ],
      }),
    );

    const runners = await listRunners();

    expect(fetchMock.mock.calls[0][0]).toBe("/v1/runners");
    expect(runners).toEqual([
      {
        runnerId: "runner_abc",
        online: true,
        harnesses: ["openai-agents"],
      },
    ]);
  });

  it("PATCHes runner_id when exactly one runner is online", async () => {
    fetchMock
      .mockResolvedValueOnce(
        mockJsonResponse({
          data: [{ runner_id: "runner_abc", online: true, harnesses: [] }],
        }),
      )
      .mockResolvedValueOnce(
        mockJsonResponse({
          id: "conv_abc",
          agent_id: "agent_xyz",
          runner_id: "runner_abc",
          host_id: "host_a1b2",
          status: "idle",
          created_at: 1704067200,
          items: [],
        }),
      );

    const session = await bindOnlyOnlineRunner("conv_abc");

    expect(fetchMock.mock.calls[0][0]).toBe("/v1/runners");
    const [url, init] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_abc");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body as string)).toEqual({ runner_id: "runner_abc" });
    expect(session?.runnerId).toBe("runner_abc");
    // host_id maps to hostId so off-sidebar sessions keep host-bound liveness.
    expect(session?.hostId).toBe("host_a1b2");
  });

  it("returns null when no runner is online", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ data: [] }));

    await expect(bindOnlyOnlineRunner("conv_abc")).resolves.toBeNull();
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("fails loudly when multiple runners are online", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        data: [
          { runner_id: "runner_a", online: true },
          { runner_id: "runner_b", online: true },
        ],
      }),
    );

    await expect(bindOnlyOnlineRunner("conv_abc")).rejects.toThrow(/2 runners are online/);
  });

  it("PATCHes reasoning_effort without runner_id", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "agent_xyz",
        status: "idle",
        created_at: 1704067200,
        items: [],
      }),
    );

    await updateSession("conv_abc", { reasoningEffort: "high" });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ reasoning_effort: "high" });
  });

  it("PATCHes model_override as snake_case", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "agent_xyz",
        status: "idle",
        created_at: 1704067200,
        items: [],
        model_override: "claude-opus-4-7",
      }),
    );

    const session = await updateSession("conv_abc", { modelOverride: "claude-opus-4-7" });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ model_override: "claude-opus-4-7" });
    // Response is parsed into camelCase modelOverride for the store.
    expect(session.modelOverride).toBe("claude-opus-4-7");
  });

  it("PATCHes model_override='default' when modelOverride is null (matches REPL /model semantics)", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "agent_xyz",
        status: "idle",
        created_at: 1704067200,
        items: [],
        model_override: null,
      }),
    );

    await updateSession("conv_abc", { modelOverride: null });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    // ``null`` is encoded as ``"default"`` on the wire — same alias the
    // server accepts on its clear path, so the REPL's ``/model default``
    // and the UI's "clear" arrive at the same backend code.
    expect(JSON.parse(init.body as string)).toEqual({ model_override: "default" });
  });

  it("PATCHes collaboration_mode as a string", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "agent_xyz",
        status: "idle",
        created_at: 1704067200,
        items: [],
        labels: { "omnigent.codex_native.collaboration_mode": "plan" },
      }),
    );

    const session = await updateSession("conv_abc", { codexPlanMode: true });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ collaboration_mode: "plan" });
    expect(session.labels?.["omnigent.codex_native.collaboration_mode"]).toBe("plan");
  });

  it("surfaces AP error messages from failed PATCHes", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse(
        {
          error: {
            code: "runner_unavailable",
            message: "Could not enter Plan mode: no live Codex runner is available.",
          },
        },
        { ok: false, status: 503 },
      ),
    );

    await expect(updateSession("conv_abc", { codexPlanMode: true })).rejects.toThrow(
      "Could not enter Plan mode",
    );
  });

  it("PATCHes cost_control_mode_override as snake_case", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "agent_xyz",
        status: "idle",
        created_at: 1704067200,
        items: [],
        cost_control_mode_override: "on",
      }),
    );

    const session = await updateSession("conv_abc", { costControlModeOverride: "on" });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ cost_control_mode_override: "on" });
    // Response is parsed into camelCase for the store's canonical refresh.
    expect(session.costControlModeOverride).toBe("on");
  });

  it("PATCHes an explicit null to clear costControlModeOverride (no clear alias)", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "agent_xyz",
        status: "idle",
        created_at: 1704067200,
        items: [],
        cost_control_mode_override: null,
      }),
    );

    await updateSession("conv_abc", { costControlModeOverride: null });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    // Unlike model_override (whose clear is the "default" alias), "off" is a
    // real value for this field — the server's clear signal is the field
    // present with a JSON null. Sending an alias here would 400.
    expect(JSON.parse(init.body as string)).toEqual({ cost_control_mode_override: null });
  });

  it("PATCHes subagent_routing_override as snake_case and reads it back", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "agent_xyz",
        status: "idle",
        created_at: 1704067200,
        items: [],
        subagent_routing_override: "on",
      }),
    );

    const session = await updateSession("conv_abc", { subagentRoutingOverride: "on" });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ subagent_routing_override: "on" });
    expect(session.subagentRoutingOverride).toBe("on");
  });

  it("PATCHes an explicit null to clear subagentRoutingOverride", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "agent_xyz",
        status: "idle",
        created_at: 1704067200,
        items: [],
        subagent_routing_override: null,
      }),
    );

    await updateSession("conv_abc", { subagentRoutingOverride: null });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    // "off" is a real value here too, so the clear signal is a JSON null. The
    // cleared session reads as Default, the same place "off" lands.
    expect(JSON.parse(init.body as string)).toEqual({ subagent_routing_override: null });
  });

  it("forwards silent:true for persistence-only session updates", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "agent_xyz",
        status: "idle",
        created_at: 1704067200,
        items: [],
        model_override: "claude-opus-4-7",
      }),
    );

    await updateSession("conv_abc", { modelOverride: "claude-opus-4-7", silent: true });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      model_override: "claude-opus-4-7",
      silent: true,
    });
  });
});

describe("getSession", () => {
  it("GETs the sessions endpoint and parses the response", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "agent_xyz",
        status: "running",
        created_at: 1704067200,
        items: [],
      }),
    );

    const session = await getSession("conv_abc");

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/sessions/conv_abc");
    expect(session.agentId).toBe("agent_xyz");
    expect(session.createdAt).toBe(1704067200);
  });

  it("url-encodes the session id", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv with space",
        agent_id: "ag",
        status: "idle",
        created_at: 0,
      }),
    );
    await getSession("conv with space");
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/sessions/conv%20with%20space");
  });

  it("routes a hostless sub-agent child by its parent's host", async () => {
    // A sub-agent child runs on its parent's runner, whose tunnel lives on the
    // replica keyed by the PARENT's host. The child row carries no host_id of
    // its own, so its session-scoped requests must key by the parent — else
    // they land keyless on the default replica and read "runner offline".
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_routing_parent",
        agent_id: "agent_xyz",
        status: "idle",
        created_at: 0,
        host_id: "host_devbox",
      }),
    );
    await getSessionSlim("conv_routing_parent");

    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_routing_child",
        agent_id: "agent_xyz",
        status: "idle",
        created_at: 0,
        host_id: null,
        kind: "sub_agent",
        parent_session_id: "conv_routing_parent",
      }),
    );
    await getSessionSlim("conv_routing_child");

    expect(getSessionHost("conv_routing_child")).toBe("host_devbox");
  });

  it("resolves the routing host through an arbitrarily deep child chain", async () => {
    // Nesting has no depth limit; only the root is host-bound.
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_deep_0",
        agent_id: "agent_xyz",
        status: "idle",
        created_at: 0,
        host_id: "host_root",
      }),
    );
    await getSessionSlim("conv_deep_0");
    for (let depth = 1; depth <= 6; depth++) {
      fetchMock.mockResolvedValueOnce(
        mockJsonResponse({
          id: `conv_deep_${depth}`,
          agent_id: "agent_xyz",
          status: "idle",
          created_at: 0,
          host_id: null,
          kind: "sub_agent",
          parent_session_id: `conv_deep_${depth - 1}`,
        }),
      );
      // oxlint-disable-next-line no-await-in-loop
      await getSessionSlim(`conv_deep_${depth}`);
    }

    expect(getSessionHost("conv_deep_6")).toBe("host_root");
  });

  it("getSessionSlim skips items, liveness, and subtree usage", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "agent_xyz",
        status: "idle",
        created_at: 1704067200,
        items: [],
        usage_included: false,
        total_cost_usd: null,
        usage_by_model: null,
      }),
    );

    const session = await getSessionSlim("conv_abc");

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/sessions/conv_abc?include_items=false&include_liveness=false&include_usage=false",
    );
    expect(session.agentId).toBe("agent_xyz");
    expect(session.items).toEqual([]);
    expect(session.usageIncluded).toBe(false);
    expect(session.totalCostUsd).toBeNull();
    expect(session.usageByModel).toBeNull();
  });

  it("getSessionSlim can request a runner-backed state refresh", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "agent_xyz",
        status: "idle",
        created_at: 1704067200,
        items: [],
      }),
    );

    await getSessionSlim("conv_abc", { refreshState: true });

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/sessions/conv_abc?include_items=false&include_liveness=false&include_usage=false&refresh_state=true",
    );
  });

  it("treats an older server's snapshot as already including usage", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "agent_xyz",
        status: "idle",
        created_at: 0,
        total_cost_usd: 4.5,
      }),
    );

    const session = await getSessionSlim("conv_abc");

    expect(session.usageIncluded).toBe(true);
    expect(session.totalCostUsd).toBe(4.5);
  });

  it("maps permission_level from the wire to permissionLevel", async () => {
    // Regression for the bug where SessionResponseWire was missing
    // permission_level — the field was on the wire but dropped at the
    // parse boundary, so child sessions appeared as "no access" in the
    // UI even when the user owned the parent.
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "ag",
        status: "idle",
        created_at: 0,
        permission_level: 4,
      }),
    );
    const session = await getSession("conv_abc");
    expect(session.permissionLevel).toBe(4);
  });

  it("treats a missing permission_level as null", async () => {
    // The server omits the field when permissions are disabled.
    // ``sessionFromWire`` must default to null so callers can lean on
    // null-vs-numeric checks without optional-chaining everywhere.
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "ag",
        status: "idle",
        created_at: 0,
      }),
    );
    const session = await getSession("conv_abc");
    expect(session.permissionLevel).toBeNull();
  });

  it("maps archived from the wire onto the snapshot", async () => {
    // The snapshot is the only archived-flag carrier for a session opened
    // directly by URL (the default sidebar list excludes archived rows).
    // Dropping it at the parse boundary made the header kebab offer
    // "Archive" on an already-archived session.
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "ag",
        status: "idle",
        created_at: 0,
        archived: true,
      }),
    );
    const session = await getSession("conv_abc");
    expect(session.archived).toBe(true);
  });

  it("treats a missing archived flag as false", async () => {
    // Older servers / recorded fixtures omit the field; absent means active.
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "ag",
        status: "idle",
        created_at: 0,
      }),
    );
    const session = await getSession("conv_abc");
    expect(session.archived).toBe(false);
  });

  it("maps parent_session_id from the wire to parentSessionId", async () => {
    // Child (sub-agent) sessions return their parent's id here so the
    // UI can mark the rail accordingly without an extra round-trip.
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_child",
        agent_id: "ag",
        status: "idle",
        created_at: 0,
        parent_session_id: "conv_parent",
      }),
    );
    const session = await getSession("conv_child");
    expect(session.parentSessionId).toBe("conv_parent");
  });

  it("maps title from the wire to the camelCase Session", async () => {
    // The sidebar's nested-child row reads ``session.title`` for the
    // display label. Without this mapping it falls back to a truncated
    // id, which is what we don't want.
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_child",
        agent_id: "ag",
        status: "idle",
        created_at: 0,
        title: "researcher:auth",
      }),
    );
    const session = await getSession("conv_child");
    expect(session.title).toBe("researcher:auth");
  });

  it("treats a missing parent_session_id as null", async () => {
    // Top-level (non-child) sessions omit the field entirely.
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_top",
        agent_id: "ag",
        status: "idle",
        created_at: 0,
      }),
    );
    const session = await getSession("conv_top");
    expect(session.parentSessionId).toBeNull();
  });
});

describe("getSessionUsage", () => {
  it("requests usage without items, liveness, or runner-backed state refresh", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv with space",
        total_cost_usd: 3.5,
        usage_by_model: {
          "model-a": { input_tokens: 10, total_cost_usd: 1 },
          "model-b": { output_tokens: 20, total_cost_usd: 2.5 },
        },
      }),
    );
    const controller = new AbortController();

    const usage = await getSessionUsage("conv with space", { signal: controller.signal });

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/sessions/conv%20with%20space?include_usage=true&include_items=false&include_liveness=false&refresh_state=false",
    );
    expect(fetchMock.mock.calls[0][1].signal).toBe(controller.signal);
    expect(usage).toEqual({
      id: "conv with space",
      totalCostUsd: 3.5,
      usageByModel: {
        "model-a": {
          inputTokens: 10,
          outputTokens: null,
          totalTokens: null,
          cacheReadInputTokens: null,
          cacheCreationInputTokens: null,
          totalCostUsd: 1,
        },
        "model-b": {
          inputTokens: null,
          outputTokens: 20,
          totalTokens: null,
          cacheReadInputTokens: null,
          cacheCreationInputTokens: null,
          totalCostUsd: 2.5,
        },
      },
    });
  });

  it("ignores other snapshot fields without replacing host routing metadata", async () => {
    setSessionHost("conv_usage_projection", "host_current");
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_usage_projection",
        agent_id: "agent_old",
        host_id: "host_old",
        status: "failed",
        created_at: 0,
        items: [],
        total_cost_usd: 3.5,
        usage_by_model: null,
      }),
    );

    try {
      expect(await getSessionUsage("conv_usage_projection")).toEqual({
        id: "conv_usage_projection",
        totalCostUsd: 3.5,
        usageByModel: null,
      });
      expect(getSessionHost("conv_usage_projection")).toBe("host_current");
    } finally {
      setSessionHost("conv_usage_projection", null);
    }
  });

  it.each([null, 0])("preserves unpriced versus priced-zero usage (%s)", async (cost) => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({ id: "conv_abc", total_cost_usd: cost, usage_by_model: null }),
    );

    expect(await getSessionUsage("conv_abc")).toEqual({
      id: "conv_abc",
      totalCostUsd: cost,
      usageByModel: null,
    });
  });

  it("rejects a failed usage read instead of synthesizing zero spend", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({}, { ok: false, status: 503 }));

    await expect(getSessionUsage("conv_abc")).rejects.toMatchObject({ status: 503 });
  });
});

describe("exportSessionTranscript", () => {
  it("writes session_meta first, then every item in ascending order", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({ id: "sess_1", object: "conversation", title: "Planning" }),
    );
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        object: "list",
        data: [
          { id: "msg_1", type: "message", role: "user" },
          { id: "msg_2", type: "message", role: "assistant" },
        ],
        first_id: "msg_1",
        last_id: "msg_2",
        has_more: false,
      }),
    );

    const jsonl = await exportSessionTranscript("sess_1");

    expect(fetchMock.mock.calls[0]![0]).toBe(
      "/v1/sessions/sess_1?include_items=false&include_liveness=false",
    );
    expect(fetchMock.mock.calls[1]![0]).toBe("/v1/sessions/sess_1/items?limit=500&order=asc");

    expect(jsonl.endsWith("\n")).toBe(true);
    const records = jsonl
      .trimEnd()
      .split("\n")
      .map((line) => JSON.parse(line) as Record<string, unknown>);
    expect(records.map((r) => r.record_type)).toEqual(["session_meta", "item", "item"]);
    expect(records[0]).toMatchObject({ id: "sess_1", title: "Planning" });
    expect(records.slice(1).map((r) => r.id)).toEqual(["msg_1", "msg_2"]);
  });

  it("pages forward with after=<last_id> until has_more is false", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ id: "sess_1" }));
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        object: "list",
        data: [{ id: "msg_1" }],
        last_id: "msg_1",
        has_more: true,
      }),
    );
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        object: "list",
        data: [{ id: "msg_2" }],
        last_id: "msg_2",
        has_more: false,
      }),
    );

    const jsonl = await exportSessionTranscript("sess_1");

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[2]![0]).toBe(
      "/v1/sessions/sess_1/items?limit=500&order=asc&after=msg_1",
    );
    const ids = jsonl
      .trimEnd()
      .split("\n")
      .slice(1)
      .map((line) => (JSON.parse(line) as { id: string }).id);
    expect(ids).toEqual(["msg_1", "msg_2"]);
  });
});

describe("fetchSessionItemsPage", () => {
  it("requests the newest page (order=desc) and returns items oldest-to-newest", async () => {
    // Server returns newest-first; the helper must reverse to chronological
    // so history renders in the same order the live stream appends.
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        object: "list",
        data: [
          {
            id: "msg_2",
            response_id: "resp_2",
            type: "message",
            role: "assistant",
            status: "completed",
            model: "agent_xyz",
            content: [{ type: "output_text", text: "second" }],
          },
          {
            id: "msg_1",
            response_id: "resp_1",
            type: "message",
            role: "user",
            status: "completed",
            content: [{ type: "input_text", text: "first" }],
          },
        ],
        first_id: "msg_2",
        last_id: "msg_1",
        has_more: true,
      }),
    );

    const page = await fetchSessionItemsPage("conv with space");

    // Reversed to chronological: oldest (msg_1) first. Dropping the
    // reverse would render the conversation backwards.
    expect(page.items.map((item) => item.id)).toEqual(["msg_1", "msg_2"]);
    // `has_more` surfaces as `hasMore` so the store can arm scroll-up loading.
    expect(page.hasMore).toBe(true);
    // One descending request at the default page size, no cursor.
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(String(fetchMock.mock.calls[0]![0])).toBe(
      `/v1/sessions/conv%20with%20space/items?limit=${SESSION_HISTORY_PAGE_SIZE}&order=desc`,
    );
  });

  it("pages backwards via an `after` cursor in descending order when olderThan is set", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        object: "list",
        data: [],
        first_id: null,
        last_id: null,
        has_more: false,
      }),
    );

    const page = await fetchSessionItemsPage("conv_abc", { olderThan: "msg_50", limit: 25 });

    expect(page.items).toEqual([]);
    expect(page.hasMore).toBe(false);
    // olderThan maps to the server's `after` cursor: under order=desc,
    // "after" means lower position = older items. Sending `before` here
    // (the pre-fix shape) would return the conversation's start instead.
    expect(String(fetchMock.mock.calls[0]![0])).toBe(
      "/v1/sessions/conv_abc/items?limit=25&order=desc&after=msg_50",
    );
  });
});

describe("postEvent", () => {
  it("POSTs the event body verbatim and returns {queued, itemId}", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ queued: true, item_id: "ci_123" }));
    const event = {
      type: "message",
      data: { role: "user", content: [{ type: "input_text", text: "hi" }] },
    };

    const out = await postEvent("conv_abc", event);

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_abc/events");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual(event);
    expect(out).toEqual({ queued: true, itemId: "ci_123" });
  });

  it("surfaces 4xx as a thrown error (does not silently swallow)", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({}, { ok: false, status: 422 }));
    await expect(postEvent("conv_abc", { type: "bogus", data: {} })).rejects.toThrow(/422/);
  });

  it("sends the local opt-out header when background titles are disabled", async () => {
    localStorage.setItem(BACKGROUND_SESSION_TITLES_STORAGE_KEY, "off");
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ queued: true }));

    await postEvent("conv_abc", { type: "message", data: {} });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("X-Omnigent-Background-Session-Titles")).toBe("off");
  });

  it("reads pending_id for a native-terminal message", async () => {
    // Native sessions return a pending-input id instead of an item_id.
    // The id identifies the snapshot's replayed bubble on rebind and is
    // the clearedPendingId the consume event carries to drop it. The
    // store does NOT swap its live optimistic bubble to this id (it
    // keeps the temp id for React-key stability); this test only asserts
    // the field is parsed off the response. Dropping the parse would
    // strand the snapshot-replayed bubble on rebind.
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({ queued: true, pending_id: "pending_abc123" }),
    );
    const out = await postEvent("conv_native", {
      type: "message",
      data: { role: "user", content: [{ type: "input_text", text: "hi" }] },
    });
    expect(out.pendingId).toBe("pending_abc123");
    expect(out.itemId).toBeUndefined();
  });
});

describe("openSessionStream", () => {
  it("opens GET /v1/sessions/{id}/stream with the supplied signal", () => {
    const signal = new AbortController().signal;
    fetchMock.mockResolvedValueOnce(mockJsonResponse({}));

    openSessionStream("conv_abc", signal);

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_abc/stream");
    expect(new Headers(init.headers).get("Accept")).toBe("text/event-stream");
    expect(init.signal).toBe(signal);
  });
});

describe("interrupt", () => {
  it.each([undefined, "codex_turn_side_1"])(
    "posts an interrupt with the optional observed response id %s",
    async (responseId) => {
      fetchMock.mockResolvedValueOnce(mockJsonResponse({ queued: false }));

      const out = await interrupt("conv_abc", responseId);

      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("/v1/sessions/conv_abc/events");
      expect(JSON.parse(init.body as string)).toEqual({
        type: "interrupt",
        data: responseId ? { response_id: responseId } : {},
      });
      expect(out.queued).toBe(false);
    },
  );
});

describe("stopSession", () => {
  it("posts {type: 'stop_session', data: {}} to the events endpoint", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ queued: false }));

    const out = await stopSession("conv_abc");

    // The server's owner gate + runner dispatch hinge on this exact
    // discriminator. A wrong type would 400 at the route or land as
    // an unknown event, making the Stop button a silent no-op.
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_abc/events");
    expect(JSON.parse(init.body as string)).toEqual({ type: "stop_session", data: {} });
    expect(out.queued).toBe(false);
  });
});

describe("continueFailedTurn", () => {
  it.each([
    { queued: true, item_id: "ci_retry" },
    { queued: true, pending_id: "pending_retry" },
  ])("submits a continuation for an accepted retry: %o", async (response) => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse(response));

    await continueFailedTurn("conv_retry");

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_retry/events");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      type: "message",
      data: {
        role: "user",
        content: [
          {
            type: "input_text",
            text: "Please continue from where you left off.",
          },
        ],
      },
    });
  });

  it("shares one in-flight continuation across error cards in the same session", async () => {
    let finishRetry: ((response: Response) => void) | undefined;
    fetchMock.mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          finishRetry = resolve;
        }),
    );

    const first = continueFailedTurn("conv_retry");
    const second = continueFailedTurn("conv_retry");

    expect(second).toBe(first);
    expect(fetchMock).toHaveBeenCalledOnce();
    finishRetry?.(mockJsonResponse({ queued: true }));
    await Promise.all([first, second]);

    fetchMock.mockResolvedValueOnce(mockJsonResponse({ queued: true }));
    await continueFailedTurn("conv_retry");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("releases a shared failed attempt so a later retry can succeed", async () => {
    let finishRetry: ((response: Response) => void) | undefined;
    fetchMock.mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          finishRetry = resolve;
        }),
    );

    const first = continueFailedTurn("conv_retry");
    const second = continueFailedTurn("conv_retry");
    const outcomes = Promise.allSettled([first, second]);
    finishRetry?.(mockJsonResponse({ queued: false, denied: true }));

    expect(await outcomes).toEqual([
      { status: "rejected", reason: new Error("The retry was blocked by a policy") },
      { status: "rejected", reason: new Error("The retry was blocked by a policy") },
    ]);
    expect(fetchMock).toHaveBeenCalledOnce();

    fetchMock.mockResolvedValueOnce(mockJsonResponse({ queued: true }));
    await continueFailedTurn("conv_retry");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("allows different sessions to retry independently", async () => {
    let finishFirst: ((response: Response) => void) | undefined;
    fetchMock.mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          finishFirst = resolve;
        }),
    );
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ queued: true }));

    const first = continueFailedTurn("conv_first");
    await continueFailedTurn("conv_second");

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/v1/sessions/conv_first/events",
      "/v1/sessions/conv_second/events",
    ]);
    finishFirst?.(mockJsonResponse({ queued: true }));
    await first;
  });

  it("rejects policy denials so the error card remains actionable", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ queued: false, denied: true }));

    await expect(continueFailedTurn("conv_retry")).rejects.toThrow(
      "The retry was blocked by a policy",
    );
  });

  it("rejects a response that did not queue a continuation", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ queued: false }));

    await expect(continueFailedTurn("conv_retry")).rejects.toThrow("The retry was not accepted");
  });

  it("propagates the server's dispatch error", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse(
        { error: { code: "runner_unavailable", message: "The host is offline" } },
        { ok: false, status: 503 },
      ),
    );

    await expect(continueFailedTurn("conv_retry")).rejects.toMatchObject({
      code: "runner_unavailable",
      message: "The host is offline",
      status: 503,
    });
  });
});

describe("approve", () => {
  it("POSTs the MCP-shape result to the elicitation's resolve URL", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ queued: false }));

    await approve("conv_abc", "elic_xyz", {
      action: "accept",
      content: { confirm: true },
    });

    // URL-based elicitation: the elicitation id rides in the URL
    // path, not the body. Pinning the exact URL guards against the
    // verdict regressing to a generic `approval` event on `/events`.
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_abc/elicitations/elic_xyz/resolve");
    expect(JSON.parse(init.body as string)).toEqual({
      action: "accept",
      content: { confirm: true },
    });
  });

  it("omits content when not supplied", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ queued: false }));
    await approve("conv_abc", "elic_xyz", { action: "decline" });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_abc/elicitations/elic_xyz/resolve");
    expect(JSON.parse(init.body as string)).toEqual({ action: "decline" });
  });
});

describe("importLocalSessions", () => {
  it("streams each session through onSession and returns the final tally", async () => {
    fetchMock.mockResolvedValueOnce(
      mockNdjsonResponse([
        JSON.stringify({ event: "session", session_id: "c1", title: "First" }),
        JSON.stringify({ event: "session", session_id: "c2", title: null }),
        JSON.stringify({ event: "done", imported: 2, already_imported: 1, failed: 0 }),
      ]),
    );

    const seen: string[] = [];
    const result = await importLocalSessions("host_1", "all", 25, (s) => seen.push(s.id));

    expect(seen).toEqual(["c1", "c2"]);
    expect(result).toEqual({
      imported: 2,
      alreadyImported: 1,
      failed: 0,
      sessions: [
        { id: "c1", title: "First" },
        { id: "c2", title: null },
      ],
      failures: [],
      skipped: 0,
      skippedSessions: [],
      // An older server's `done` (no total / complete) still reads as complete.
      total: null,
      complete: true,
      error: null,
    });
    // Hits the streaming endpoint with the snake_case body.
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/imports/local/stream");
    expect(JSON.parse(init.body as string)).toEqual({
      host_id: "host_1",
      source: "all",
      limit: 25,
    });
  });

  it("collects per-session failure reasons from failed events and the tally", async () => {
    fetchMock.mockResolvedValueOnce(
      mockNdjsonResponse([
        JSON.stringify({ event: "session", session_id: "c1", title: "Good" }),
        JSON.stringify({
          event: "failed",
          external_session_id: "bad-1",
          source: "codex",
          reason: "No visible messages to import.",
        }),
        JSON.stringify({
          event: "done",
          imported: 1,
          already_imported: 0,
          failed: 1,
          failures: [
            { external_session_id: "bad-1", source: "codex", reason: "No visible messages." },
          ],
        }),
      ]),
    );

    const result = await importLocalSessions("host_1", "all", 25);

    expect(result.imported).toBe(1);
    expect(result.failed).toBe(1);
    // A failure from a server that predates codes: no code, offered for retry.
    expect(result.failures).toEqual([
      {
        externalSessionId: "bad-1",
        source: "codex",
        reason: "No visible messages to import.",
        code: null,
        retryable: true,
        errorId: null,
      },
    ]);
  });

  it("keeps the tally and sessions when an error event arrives (old server: no code)", async () => {
    fetchMock.mockResolvedValueOnce(
      mockNdjsonResponse([
        JSON.stringify({ event: "session", session_id: "c1", title: "First" }),
        JSON.stringify({ event: "error", message: "host stalled mid-import" }),
        JSON.stringify({ event: "done", imported: 1, already_imported: 0, failed: 0 }),
      ]),
    );

    const seen: string[] = [];
    const result = await importLocalSessions("h", "claude", 10, (s) => seen.push(s.id));

    // The session that streamed before the error was still handed to the caller.
    expect(seen).toEqual(["c1"]);
    expect(result.imported).toBe(1);
    expect(result.sessions).toEqual([{ id: "c1", title: "First" }]);
    expect(result.complete).toBe(false);
    expect(result.error).toEqual({
      code: null,
      message: "host stalled mid-import",
      retryable: true,
      errorId: null,
      fixCommands: [],
      hostName: null,
    });
  });

  it("reads code, retryable, error id and details from the error event", async () => {
    fetchMock.mockResolvedValueOnce(
      mockNdjsonResponse([
        JSON.stringify({ event: "progress", done: 3, total: 10 }),
        JSON.stringify({ event: "session", session_id: "c1", title: "One" }),
        JSON.stringify({
          event: "failed",
          external_session_id: "big-1",
          source: "claude",
          reason: "This session is too large to import.",
          code: "session_too_large",
          retryable: false,
        }),
        JSON.stringify({
          event: "error",
          error_id: "err_abc",
          message: "mac-laptop disconnected after 3 of 10 sessions.",
          code: "host_disconnected",
          retryable: true,
          host_name: "mac-laptop",
          processed: 3,
          total: 10,
        }),
        JSON.stringify({
          event: "done",
          imported: 1,
          already_imported: 1,
          failed: 1,
          failures: [],
          total: 10,
          complete: false,
        }),
      ]),
    );

    const result = await importLocalSessions("host_1", "all", 25);

    expect(result).toMatchObject({
      imported: 1,
      alreadyImported: 1,
      failed: 1,
      total: 10,
      complete: false,
      error: {
        code: "host_disconnected",
        message: "mac-laptop disconnected after 3 of 10 sessions.",
        retryable: true,
        errorId: "err_abc",
        hostName: "mac-laptop",
      },
    });
    expect(result.failures).toEqual([
      {
        externalSessionId: "big-1",
        source: "claude",
        reason: "This session is too large to import.",
        code: "session_too_large",
        retryable: false,
        errorId: null,
      },
    ]);
  });

  it("passes fix_commands through for the missing-SQLite error", async () => {
    fetchMock.mockResolvedValueOnce(
      mockNdjsonResponse([
        JSON.stringify({
          event: "error",
          error_id: "err_sql",
          message: "Your machine's Python was built without SQLite.",
          code: "host_python_missing_sqlite",
          retryable: false,
          fix_commands: [
            { label: "macOS", command: "brew install sqlite && pyenv install --force 3.12" },
            "sudo apt-get install libsqlite3-dev",
            { label: "no command" },
            { label: "blank", command: " " },
            7,
            "",
          ],
        }),
        JSON.stringify({ event: "done", imported: 0, already_imported: 0, failed: 0 }),
      ]),
    );

    const result = await importLocalSessions("host_1", "all", 25);

    expect(result.error).toMatchObject({
      code: "host_python_missing_sqlite",
      retryable: false,
      // {label, command} entries; an older server's bare strings have no label;
      // entries without a command are dropped rather than rendered.
      fixCommands: [
        { label: "macOS", command: "brew install sqlite && pyenv install --force 3.12" },
        { label: null, command: "sudo apt-get install libsqlite3-dev" },
      ],
    });
  });

  it("moves an inline error id out of the reason and message", async () => {
    fetchMock.mockResolvedValueOnce(
      mockNdjsonResponse([
        JSON.stringify({
          event: "failed",
          external_session_id: "s1",
          source: "claude",
          reason:
            "This session couldn't be saved because of an internal error. Error ID: err_0a1b.",
          code: "internal",
          retryable: true,
        }),
        JSON.stringify({
          event: "error",
          error_id: "err_ffee",
          message: "The local session import stopped unexpectedly. Error ID: err_ffee.",
          code: "internal",
          retryable: true,
        }),
        JSON.stringify({ event: "done", imported: 0, already_imported: 0, failed: 1 }),
      ]),
    );

    const result = await importLocalSessions("host_1", "all", 25);

    expect(result.failures[0]).toMatchObject({
      reason: "This session couldn't be saved because of an internal error.",
      errorId: "err_0a1b",
    });
    expect(result.error).toMatchObject({
      message: "The local session import stopped unexpectedly.",
      errorId: "err_ffee",
    });
  });

  it("keeps a reason whose trailing id differs from the error_id field", async () => {
    fetchMock.mockResolvedValueOnce(
      mockNdjsonResponse([
        JSON.stringify({
          event: "failed",
          reason: "Failed. Error ID: err_aaaa.",
          error_id: "err_bbbb",
          code: "internal",
        }),
        JSON.stringify({ event: "done", imported: 0, already_imported: 0, failed: 1 }),
      ]),
    );

    const result = await importLocalSessions("host_1", "all", 25);

    expect(result.failures[0]).toMatchObject({
      reason: "Failed. Error ID: err_aaaa.",
      errorId: "err_bbbb",
    });
  });

  it.each([
    ["session_too_large", false],
    ["session_unreadable", false],
    ["session_save_timeout", true],
    ["encryption_unavailable", true],
    ["host_python_missing_sqlite", false],
    ["internal", true],
    ["some_future_code", true],
  ])("derives retryable for a failed %s event without the flag", async (code, retryable) => {
    fetchMock.mockResolvedValueOnce(
      mockNdjsonResponse([
        JSON.stringify({ event: "failed", external_session_id: "s1", reason: "Nope.", code }),
        JSON.stringify({ event: "done", imported: 0, already_imported: 0, failed: 1 }),
      ]),
    );

    const result = await importLocalSessions("host_1", "all", 25);

    expect(result.failures[0]).toMatchObject({ code, retryable, reason: "Nope." });
  });

  it.each([
    ["host_offline", true],
    ["host_unreachable", true],
    ["host_disconnected", true],
    ["host_unresponsive", true],
    ["time_limit_reached", true],
    ["host_python_missing_sqlite", false],
    ["invalid_request", false],
    ["internal", true],
  ])("derives retryable for a %s error event without the flag", async (code, retryable) => {
    fetchMock.mockResolvedValueOnce(
      mockNdjsonResponse([
        JSON.stringify({ event: "error", message: "Stopped.", code }),
        JSON.stringify({ event: "done", imported: 0, already_imported: 0, failed: 0 }),
      ]),
    );

    const result = await importLocalSessions("host_1", "all", 25);

    expect(result.error).toMatchObject({ code, retryable, message: "Stopped." });
    expect(result.complete).toBe(false);
  });

  it("reports progress events and keeps the host's total", async () => {
    fetchMock.mockResolvedValueOnce(
      mockNdjsonResponse([
        JSON.stringify({ event: "progress", done: 0, total: 20 }),
        JSON.stringify({ event: "session", session_id: "c1", title: "One" }),
        JSON.stringify({ event: "progress", done: 7, total: 20 }),
        JSON.stringify({ event: "progress", done: "bad" }),
        JSON.stringify({ event: "progress", done: 8, total: null }),
        JSON.stringify({ event: "done", imported: 1, already_imported: 7, failed: 0 }),
      ]),
    );

    const onProgress = vi.fn();
    const result = await importLocalSessions("host_1", "all", 25, undefined, undefined, {
      onProgress,
    });

    expect(onProgress.mock.calls.map((c) => c[0])).toEqual([
      { done: 0, total: 20 },
      { done: 7, total: 20 },
      { done: 8, total: null },
    ]);
    // `done` carried no total, so the last progress event's (null) stands.
    expect(result.total).toBeNull();
    expect(result.complete).toBe(true);
  });

  it("maps a body whose reader rejects mid-stream to stream_interrupted, keeping results", async () => {
    fetchMock.mockResolvedValueOnce(
      mockBrokenNdjsonResponse([
        JSON.stringify({ event: "progress", done: 2, total: 9 }),
        JSON.stringify({ event: "session", session_id: "c1", title: "One" }),
        JSON.stringify({ event: "session", session_id: "c2", title: "Two" }),
        JSON.stringify({ event: "progress", done: 3, total: 9 }),
      ]),
    );

    const seen: string[] = [];
    const result = await importLocalSessions("host_1", "all", 25, (s) => seen.push(s.id));

    expect(seen).toEqual(["c1", "c2"]);
    expect(result).toMatchObject({
      imported: 2,
      alreadyImported: 0,
      failed: 0,
      total: 9,
      complete: false,
      error: {
        code: "stream_interrupted",
        retryable: true,
        message:
          "The connection to Omnigent dropped after 3 sessions. Import again to continue — sessions already imported are skipped.",
      },
    });
  });

  it("maps truncated NDJSON (cut mid-line, no done) to stream_interrupted", async () => {
    fetchMock.mockResolvedValueOnce(
      mockBrokenNdjsonResponse(
        [
          JSON.stringify({ event: "session", session_id: "c1", title: "One" }),
          JSON.stringify({ event: "failed", external_session_id: "x", reason: "Unreadable." }),
        ],
        { partialTail: '{"event":"session","session_id":"c2","ti', rejectWith: null },
      ),
    );

    const result = await importLocalSessions("host_1", "all", 25);

    // The half line is dropped; what arrived intact is kept.
    expect(result.sessions).toEqual([{ id: "c1", title: "One" }]);
    expect(result.failures).toHaveLength(1);
    expect(result.failed).toBe(1);
    expect(result.error?.code).toBe("stream_interrupted");
    expect(result.error?.message).toContain("dropped after 2 sessions");
  });

  it("maps a stream that ends cleanly but without done to stream_interrupted", async () => {
    fetchMock.mockResolvedValueOnce(
      mockNdjsonResponse([JSON.stringify({ event: "session", session_id: "c1", title: "One" })]),
    );

    const result = await importLocalSessions("host_1", "all", 25);

    expect(result.error?.code).toBe("stream_interrupted");
    expect(result.error?.message).toContain("dropped after 1 session.");
    expect(result.complete).toBe(false);
  });

  it("prefers the server's error over stream_interrupted when the body breaks after it", async () => {
    fetchMock.mockResolvedValueOnce(
      mockBrokenNdjsonResponse([
        JSON.stringify({
          event: "error",
          message: "Imported 4 of 9 before the time limit.",
          code: "time_limit_reached",
          retryable: true,
        }),
      ]),
    );

    const result = await importLocalSessions("host_1", "all", 25);

    expect(result.error?.code).toBe("time_limit_reached");
  });

  it("reports a fetch that never got a response as stream_interrupted, not a raw TypeError", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));

    const result = await importLocalSessions("host_1", "all", 25);

    expect(result.complete).toBe(false);
    expect(result.error).toMatchObject({ code: "stream_interrupted", retryable: true });
    expect(result.error?.message).toContain("before any sessions were imported");
    expect(result.error?.message).not.toContain("Failed to fetch");
  });

  it("falls back to done.failures when no failed events streamed", async () => {
    fetchMock.mockResolvedValueOnce(
      mockNdjsonResponse([
        JSON.stringify({
          event: "done",
          imported: 0,
          already_imported: 0,
          failed: 1,
          failures: [
            {
              external_session_id: "s9",
              source: "pi",
              reason: "Saving this session timed out.",
              code: "session_save_timeout",
              retryable: true,
            },
            "junk",
          ],
          complete: true,
        }),
      ]),
    );

    const result = await importLocalSessions("host_1", "all", 25);

    expect(result.failures).toEqual([
      {
        externalSessionId: "s9",
        source: "pi",
        reason: "Saving this session timed out.",
        code: "session_save_timeout",
        retryable: true,
        errorId: null,
      },
    ]);
  });

  it("throws an ApiError carrying import_code, retryable and details for a pre-stream 409", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse(
        {
          error: {
            code: "conflict",
            message: "mac-laptop is offline (last seen 5 minutes ago).",
            import_code: "host_offline",
            retryable: true,
            host_name: "mac-laptop",
            last_seen_seconds: 300,
          },
        },
        { ok: false, status: 409 },
      ),
    );

    const err = await importLocalSessions("host_1", "all", 25).catch((e: unknown) => e);

    expect(err).toMatchObject({
      status: 409,
      code: "conflict",
      importCode: "host_offline",
      retryable: true,
      details: { host_name: "mac-laptop", last_seen_seconds: 300 },
    });
    expect(importErrorFromException(err)).toEqual({
      code: "host_offline",
      message: "mac-laptop is offline (last seen 5 minutes ago).",
      retryable: true,
      errorId: null,
      fixCommands: [],
      hostName: "mac-laptop",
    });
  });

  it("sends an exact session ID with its harness", async () => {
    fetchMock.mockResolvedValueOnce(
      mockNdjsonResponse([
        JSON.stringify({ event: "session", session_id: "c1", title: "Exact" }),
        JSON.stringify({ event: "done", imported: 1, already_imported: 0, failed: 0 }),
      ]),
    );

    await importLocalSessions("host_1", "codex", 25, undefined, "session-exact");

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({
      host_id: "host_1",
      source: "codex",
      limit: 25,
      session_id: "session-exact",
    });
  });

  it("does not fall back to a server that cannot distinguish an exact import", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({}, { ok: false, status: 404 }));

    await expect(
      importLocalSessions("host_1", "codex", 25, undefined, "session-exact"),
    ).rejects.toThrow("Direct session import is not supported");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("falls back to the buffered endpoint when the stream endpoint 404s", async () => {
    // Old server: the streaming endpoint is absent, so the client retries the
    // buffered one and delivers every session through onSession at once.
    fetchMock.mockResolvedValueOnce(mockJsonResponse({}, { ok: false, status: 404 }));
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        imported: 2,
        already_imported: 1,
        failed: 0,
        sessions: [
          { session_id: "c1", title: "First" },
          { session_id: "c2", title: null },
        ],
      }),
    );

    const seen: string[] = [];
    const result = await importLocalSessions("host_1", "all", 25, (s) => seen.push(s.id));

    expect(seen).toEqual(["c1", "c2"]);
    expect(result).toEqual({
      imported: 2,
      alreadyImported: 1,
      failed: 0,
      sessions: [
        { id: "c1", title: "First" },
        { id: "c2", title: null },
      ],
      failures: [],
      skipped: 0,
      skippedSessions: [],
      total: null,
      complete: true,
      error: null,
    });
    // First the stream endpoint (404), then the buffered fallback.
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/imports/local/stream");
    expect(fetchMock.mock.calls[1][0]).toBe("/v1/imports/local");
  });
});

describe("importLocalSessions skipped (empty) sessions", () => {
  const emptyWire = (id: string) => ({
    external_session_id: id,
    source: "codex",
    reason: `Codex session '${id}' has no importable history`,
    code: "session_empty",
    retryable: false,
    error_id: null,
  });
  const emptyRef = (id: string) => ({
    externalSessionId: id,
    source: "codex",
    reason: `Codex session '${id}' has no importable history`,
    code: "session_empty",
    retryable: false,
    errorId: null,
  });

  it("reports skipped events apart from failures", async () => {
    fetchMock.mockResolvedValueOnce(
      mockNdjsonResponse([
        JSON.stringify({ event: "session", session_id: "c1", title: "Good" }),
        JSON.stringify({
          event: "failed",
          external_session_id: "bad-1",
          source: "codex",
          reason: "This session's transcript could not be read.",
          code: "session_unreadable",
          retryable: false,
        }),
        JSON.stringify({ event: "skipped", ...emptyWire("empty-1") }),
        JSON.stringify({ event: "skipped", ...emptyWire("empty-2") }),
        JSON.stringify({
          event: "done",
          imported: 1,
          already_imported: 0,
          failed: 1,
          failures: [],
          skipped: 2,
          skipped_sessions: [emptyWire("empty-1"), emptyWire("empty-2")],
          total: 4,
          complete: true,
        }),
      ]),
    );

    const result = await importLocalSessions("host_1", "all", 25);

    expect(result.failed).toBe(1);
    expect(result.failures.map((f) => f.externalSessionId)).toEqual(["bad-1"]);
    expect(result.skipped).toBe(2);
    expect(result.skippedSessions).toEqual([emptyRef("empty-1"), emptyRef("empty-2")]);
    expect(result.complete).toBe(true);
  });

  it("reads skipped sessions from done when no skipped events were streamed", async () => {
    fetchMock.mockResolvedValueOnce(
      mockNdjsonResponse([
        JSON.stringify({
          event: "done",
          imported: 0,
          already_imported: 3,
          failed: 0,
          failures: [],
          skipped: 1,
          skipped_sessions: [emptyWire("empty-1")],
        }),
      ]),
    );

    const result = await importLocalSessions("host_1", "codex", 25);

    expect(result.skipped).toBe(1);
    expect(result.skippedSessions).toEqual([emptyRef("empty-1")]);
    expect(result.failed).toBe(0);
  });

  it("moves session_empty failures out of failed for a server without skipped", async () => {
    // A server that predates `skipped` lists an empty session as a failure
    // (and, not knowing the code, as retryable).
    fetchMock.mockResolvedValueOnce(
      mockNdjsonResponse([
        JSON.stringify({ event: "failed", ...emptyWire("empty-1"), retryable: true }),
        JSON.stringify({
          event: "failed",
          external_session_id: "big-1",
          source: "claude",
          reason: "too large",
          code: "session_too_large",
          retryable: false,
        }),
        JSON.stringify({ event: "done", imported: 2, already_imported: 0, failed: 2 }),
      ]),
    );

    const result = await importLocalSessions("host_1", "all", 25);

    expect(result.failed).toBe(1);
    expect(result.failures.map((f) => f.code)).toEqual(["session_too_large"]);
    expect(result.skipped).toBe(1);
    expect(result.skippedSessions.map((f) => f.externalSessionId)).toEqual(["empty-1"]);
  });

  it("keeps an old server's uncoded failures as failures", async () => {
    fetchMock.mockResolvedValueOnce(
      mockNdjsonResponse([
        JSON.stringify({
          event: "failed",
          external_session_id: "empty-1",
          source: "codex",
          reason: "Codex session 'empty-1' has no importable history",
        }),
        JSON.stringify({ event: "done", imported: 0, already_imported: 0, failed: 1 }),
      ]),
    );

    const result = await importLocalSessions("host_1", "all", 25);

    expect(result.failed).toBe(1);
    expect(result.skipped).toBe(0);
    expect(result.skippedSessions).toEqual([]);
  });

  it("reads skipped sessions from the buffered fallback", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({}, { ok: false, status: 404 }));
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        imported: 1,
        already_imported: 0,
        failed: 0,
        sessions: [{ session_id: "c1", title: "One" }],
        failures: [],
        skipped: 1,
        skipped_sessions: [emptyWire("empty-1")],
      }),
    );

    const result = await importLocalSessions("host_1", "all", 25);

    expect(result.imported).toBe(1);
    expect(result.skipped).toBe(1);
    expect(result.skippedSessions).toEqual([emptyRef("empty-1")]);
  });
});

describe("importLocalSessions host read failures", () => {
  it("treats host_read_failed as not retryable and keeps the host's message", async () => {
    fetchMock.mockResolvedValueOnce(
      mockNdjsonResponse([
        JSON.stringify({
          event: "error",
          code: "host_read_failed",
          message: "Codex sessions could not be listed on this machine.",
          error_id: "err_1",
        }),
        JSON.stringify({
          event: "done",
          imported: 0,
          already_imported: 0,
          failed: 0,
          complete: false,
        }),
      ]),
    );

    const result = await importLocalSessions("host_1", "codex", 25);

    expect(result.complete).toBe(false);
    expect(result.error).toMatchObject({
      code: "host_read_failed",
      message: "Codex sessions could not be listed on this machine.",
      retryable: false,
    });
  });
});

describe("importErrorFromException", () => {
  function apiError(body: unknown, status: number): Promise<unknown> {
    return apiErrorFromResponse(mockJsonResponse(body, { ok: false, status, statusText: "Error" }));
  }

  it("maps a 422 validation error to a non-retryable invalid_request", async () => {
    const err = await apiError(
      {
        detail: [
          {
            type: "less_than_equal",
            loc: ["body", "limit"],
            msg: "Input should be less than or equal to 100",
          },
        ],
      },
      422,
    );
    expect(importErrorFromException(err)).toMatchObject({
      code: "invalid_request",
      message: "limit: Input should be less than or equal to 100",
      retryable: false,
    });
  });

  it("reads error_id and fix_commands from the HTTP error body", async () => {
    const err = await apiError(
      {
        error: {
          code: "internal_error",
          message: "Import stopped because of an internal error.",
          import_code: "internal",
          retryable: true,
          error_id: "err_123",
          fix_commands: [{ label: "Restart the host", command: "omnigent host" }],
        },
      },
      500,
    );
    expect(importErrorFromException(err)).toMatchObject({
      code: "internal",
      retryable: true,
      errorId: "err_123",
      fixCommands: [{ label: "Restart the host", command: "omnigent host" }],
    });
  });

  it("gives a wrong_replica from an older server a readable message", async () => {
    const err = await apiError(
      { error: { code: "wrong_replica", message: "host is on another replica" } },
      400,
    );
    expect(importErrorFromException(err, { hostName: "studio-mac" })).toMatchObject({
      code: "host_unreachable",
      retryable: true,
      message: "Couldn't reach “studio-mac”'s connection. Try again in a few seconds.",
      hostName: "studio-mac",
    });
    expect(importErrorFromException(err).message).toBe(
      "Couldn't reach your machine's connection. Try again in a few seconds.",
    );
  });

  it("uses a newer server's wrong_replica message and import code", async () => {
    const err = await apiError(
      {
        error: {
          code: "wrong_replica",
          message: "Couldn't reach “laptop”'s connection. Try again in a few seconds.",
          import_code: "host_unreachable",
          retryable: true,
          host_name: "laptop",
        },
      },
      400,
    );
    expect(importErrorFromException(err, { hostName: "other" })).toMatchObject({
      code: "host_unreachable",
      retryable: true,
      message: "Couldn't reach “laptop”'s connection. Try again in a few seconds.",
      hostName: "laptop",
    });
  });

  it.each([
    [409, true],
    [500, true],
    [503, true],
    [400, false],
    [403, false],
  ])("treats a code-less (old server) %i as retryable=%s", async (status, retryable) => {
    const err = await apiError(
      { error: { code: "conflict", message: "Host is offline." } },
      status,
    );
    expect(importErrorFromException(err)).toMatchObject({
      code: null,
      message: "Host is offline.",
      retryable,
    });
  });

  it("keeps an unexpected error's message", () => {
    expect(importErrorFromException(new Error("boom"))).toMatchObject({
      code: null,
      message: "boom",
      retryable: true,
    });
    expect(importErrorFromException("nope").message).toBe("Import failed. Try again.");
  });

  it("knows each contract code's retry semantics", () => {
    expect(importCodeIsRetryable("session_too_large")).toBe(false);
    expect(importCodeIsRetryable("already_imported")).toBe(false);
    expect(importCodeIsRetryable("stream_interrupted")).toBe(true);
    expect(importCodeIsRetryable(null)).toBe(true);
  });
});

describe("validationDetailMessage", () => {
  it.each([
    [
      [{ loc: ["body", "limit"], msg: "Input should be less than or equal to 100" }],
      "limit: Input should be less than or equal to 100",
    ],
    // A model-level validator has no field: no prefix, and pydantic's kind prefix is dropped.
    [
      [{ loc: ["body"], msg: "Value error, an exact session import requires a specific harness" }],
      "an exact session import requires a specific harness",
    ],
    [
      [{ loc: ["query", "source"], msg: "Input should be 'claude' or 'codex'" }],
      "source: Input should be 'claude' or 'codex'",
    ],
    // Nested paths join with dots; an overlong one is dropped rather than shown.
    [
      [{ loc: ["body", "items", 3, "type"], msg: "Field required" }],
      "items.3.type: Field required",
    ],
    [[{ loc: ["body", "a_very_long_field_name", "another_long_segment"], msg: "Bad" }], "Bad"],
    [[{ msg: "No location" }], "No location"],
  ])("formats %j", (detail, expected) => {
    expect(validationDetailMessage(detail)).toBe(expected);
  });

  it.each([null, "text", {}, [], [{ loc: ["body"] }]])("returns null for %j", (detail) => {
    expect(validationDetailMessage(detail)).toBeNull();
  });
});
