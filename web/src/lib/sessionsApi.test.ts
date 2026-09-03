// Unit tests for `sessionsApi.ts` — happy-path POSTs with mocked
// `fetch`, plus argument-shape pins for `interrupt` and `approve`.
//
// These tests primarily guard the camelCase TS ↔ snake_case wire
// boundary: a regression here would mean the store hits an endpoint
// with the wrong field names, which the server would 422 with no
// useful client-side error trail.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  approve,
  bindOnlyOnlineRunner,
  createSession,
  fetchSessionItemsPage,
  forkSession,
  getSession,
  getSessionSlim,
  importLocalSessions,
  interrupt,
  listRunners,
  openSessionStream,
  postEvent,
  SESSION_HISTORY_PAGE_SIZE,
  stopSession,
  updateSession,
} from "./sessionsApi";

function mockJsonResponse(body: unknown, init?: { ok?: boolean; status?: number }): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: "OK",
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

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
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
      status: "idle",
      createdAt: 1704067200,
      title: null,
      items: [],
      queuedItems: undefined,
      contextWindow: undefined,
      labels: undefined,
      lastTaskError: undefined,
      lastTotalTokens: undefined,
      totalCostUsd: undefined,
      usageByModel: null,
      llmModel: undefined,
      harness: null,
      modelOverride: undefined,
      costControlModeOverride: undefined,
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
      skills: [],
      codexModelOptions: [],
      terminalPending: false,
      sandboxStatus: null,
      mcpStartup: null,
      activeResponseId: null,
      workspace: null,
      gitBranch: null,
    });
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

    await forkSession("conv_src", "My clone");

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

    await forkSession("conv_src", undefined, undefined, undefined, {
      modelOverride: "opus",
      reasoningEffort: "high",
      terminalLaunchArgs: ["--permission-mode", "auto"],
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
    await forkSession("conv_src", undefined, undefined, undefined, {});

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({});
  });

  it("surfaces a non-ok response as a thrown error (e.g. 403 no access)", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({}, { ok: false, status: 403 }));
    await expect(forkSession("conv_src")).rejects.toThrow(/403/);
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

  it("forwards silent:true so bind-time auto-apply skips runner forward", async () => {
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

  it("getSessionSlim requests the snapshot without items or liveness", async () => {
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({
        id: "conv_abc",
        agent_id: "agent_xyz",
        status: "idle",
        created_at: 1704067200,
        items: [],
      }),
    );

    const session = await getSessionSlim("conv_abc");

    expect(fetchMock).toHaveBeenCalledOnce();
    // The two skipped reads are the most expensive steps of the server's
    // snapshot build; the chat surface loads items via /items and liveness
    // via the /health poll, so it opts out of both.
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/sessions/conv_abc?include_items=false&include_liveness=false",
    );
    expect(session.agentId).toBe("agent_xyz");
    expect(session.items).toEqual([]);
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
      "/v1/sessions/conv_abc?include_items=false&include_liveness=false&refresh_state=true",
    );
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
  it("posts {type: 'interrupt', data: {}} to the events endpoint", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ queued: false }));

    const out = await interrupt("conv_abc");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_abc/events");
    expect(JSON.parse(init.body as string)).toEqual({ type: "interrupt", data: {} });
    expect(out.queued).toBe(false);
  });
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

  it("throws the server's message on a mid-stream error, keeping delivered sessions", async () => {
    fetchMock.mockResolvedValueOnce(
      mockNdjsonResponse([
        JSON.stringify({ event: "session", session_id: "c1", title: "First" }),
        JSON.stringify({ event: "error", message: "host stalled mid-import" }),
        JSON.stringify({ event: "done", imported: 1, already_imported: 0, failed: 0 }),
      ]),
    );

    const seen: string[] = [];
    await expect(importLocalSessions("h", "claude", 10, (s) => seen.push(s.id))).rejects.toThrow(
      "host stalled mid-import",
    );
    // The session that streamed before the error was still handed to the caller.
    expect(seen).toEqual(["c1"]);
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
    });
    // First the stream endpoint (404), then the buffered fallback.
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/imports/local/stream");
    expect(fetchMock.mock.calls[1][0]).toBe("/v1/imports/local");
  });
});
