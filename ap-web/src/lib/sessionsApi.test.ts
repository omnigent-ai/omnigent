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
  fetchInitialHistoryWindow,
  fetchSessionItemsPage,
  forkSession,
  getSession,
  getSessionSlim,
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
      todos: [],
      skills: [],
      codexModelOptions: [],
      terminalPending: false,
      sandboxStatus: null,
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

describe("fetchInitialHistoryWindow", () => {
  // Wire builders. The server returns one page newest-first (order=desc);
  // the helper reverses each page to chronological and prepends older
  // pages, so the assertions below track item ids, not raw wire order.
  function userWire(id: string, opts: { meta?: boolean } = {}) {
    return {
      id,
      response_id: `resp_${id}`,
      type: "message",
      role: "user",
      status: "completed",
      content: [{ type: "input_text", text: id }],
      ...(opts.meta ? { is_meta: true } : {}),
    };
  }
  function asstWire(id: string) {
    // Stands in for any non-user turn item (assistant text, tool call,
    // tool output) — only real user prompts count toward the boundary.
    return {
      id,
      response_id: `resp_${id}`,
      type: "message",
      role: "assistant",
      status: "completed",
      model: "agent_xyz",
      content: [{ type: "output_text", text: id }],
    };
  }
  function pageBody(dataNewestFirst: Array<{ id: string }>, hasMore: boolean): Response {
    return mockJsonResponse({
      object: "list",
      data: dataNewestFirst,
      first_id: dataNewestFirst[0]?.id ?? null,
      last_id: dataNewestFirst[dataNewestFirst.length - 1]?.id ?? null,
      has_more: hasMore,
    });
  }

  it("stops after one fetch when the first page already holds 2+ user prompts", async () => {
    // 20 items (the floor) with two user prompts → the previous user
    // message is already on screen, so no extra request is needed. This
    // is the common case and must stay as cheap as fetchSessionItemsPage.
    const fillers = Array.from({ length: 18 }, (_, i) => asstWire(`a${i}`));
    fetchMock.mockResolvedValueOnce(
      pageBody([...fillers, userWire("u_last"), userWire("u_prev")], true),
    );

    const page = await fetchInitialHistoryWindow("conv_abc");

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(page.items).toHaveLength(SESSION_HISTORY_PAGE_SIZE);
    expect(page.hasMore).toBe(true);
    // Single descending request, no cursor — same shape as the plain page.
    expect(String(fetchMock.mock.calls[0]![0])).toBe(
      `/v1/sessions/conv_abc/items?limit=${SESSION_HISTORY_PAGE_SIZE}&order=desc`,
    );
  });

  it("pages backward until the previous user message is included (long single turn)", async () => {
    // A long turn: one user prompt followed by 19 tool/assistant items, so
    // the first 20-item page contains only ONE user prompt. The previous
    // prompt lives in the next page; the helper must fetch it.
    const turnFillers = Array.from({ length: 19 }, (_, i) => asstWire(`t${i}`));
    // Page 1 newest-first: fillers then the turn's prompt as the oldest item.
    fetchMock.mockResolvedValueOnce(pageBody([...turnFillers, userWire("u_last")], true));
    // Page 2 (older): the previous exchange.
    fetchMock.mockResolvedValueOnce(pageBody([asstWire("a_prev"), userWire("u_prev")], true));

    const page = await fetchInitialHistoryWindow("conv_abc");

    expect(fetchMock).toHaveBeenCalledTimes(2);
    // Window now starts at the previous user prompt and runs to the newest item.
    expect(page.items[0]!.id).toBe("u_prev");
    expect(page.items).toHaveLength(22);
    expect(page.hasMore).toBe(true);
    // Second fetch pages older via the oldest loaded id (u_last) as `after`.
    expect(String(fetchMock.mock.calls[1]![0])).toBe(
      `/v1/sessions/conv_abc/items?limit=${SESSION_HISTORY_PAGE_SIZE}&order=desc&after=u_last`,
    );
  });

  it("keeps fetching into a last turn longer than one page until its prompt is reached", async () => {
    // The newest turn itself spans more than SESSION_HISTORY_PAGE_SIZE
    // items, so the first page is ALL assistant/tool items — zero user
    // prompts. The helper must page past the full turn to surface the
    // last user message (the prompt that started it), then one more to
    // the previous prompt, rather than opening to a response with no
    // visible prompt above it.
    const tail = Array.from({ length: 20 }, (_, i) => asstWire(`tail${i}`));
    fetchMock.mockResolvedValueOnce(pageBody(tail, true)); // page 1: no user prompt
    // Page 2: the rest of the long turn, then the prompt that started it.
    const head = Array.from({ length: 5 }, (_, i) => asstWire(`head${i}`));
    fetchMock.mockResolvedValueOnce(pageBody([...head, userWire("u_last")], true));
    // Page 3: the previous exchange.
    fetchMock.mockResolvedValueOnce(pageBody([asstWire("a_prev"), userWire("u_prev")], true));

    const page = await fetchInitialHistoryWindow("conv_abc");

    expect(fetchMock).toHaveBeenCalledTimes(3);
    // The long turn's own prompt is included even though it sat beyond the
    // first page...
    expect(page.items.some((i) => i.id === "u_last")).toBe(true);
    // ...and the window still extends back to the previous prompt.
    expect(page.items[0]!.id).toBe("u_prev");
  });

  it("stops at the start of a short conversation without spinning", async () => {
    // Only one user prompt exists; has_more=false means there is nothing
    // older to fetch. Must return what it has rather than loop forever
    // chasing a second prompt that doesn't exist.
    fetchMock.mockResolvedValueOnce(pageBody([asstWire("a_1"), userWire("u_1")], false));

    const page = await fetchInitialHistoryWindow("conv_abc");

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(page.items.map((i) => i.id)).toEqual(["u_1", "a_1"]);
    expect(page.hasMore).toBe(false);
  });

  it("does not count meta user items toward the boundary", async () => {
    // Injected context (is_meta) carries role:"user" but is not a real
    // prompt. A full page of meta-user items must NOT satisfy the
    // boundary — the helper keeps paging to find genuine prompts.
    const metaUsers = Array.from({ length: 20 }, (_, i) => userWire(`m${i}`, { meta: true }));
    fetchMock.mockResolvedValueOnce(pageBody(metaUsers, true));
    fetchMock.mockResolvedValueOnce(pageBody([userWire("u_last"), userWire("u_prev")], true));

    const page = await fetchInitialHistoryWindow("conv_abc");

    expect(fetchMock).toHaveBeenCalledTimes(2);
    // Both real prompts present; the meta page alone would have stopped a
    // count-only window short of any real prompt.
    expect(page.items.filter((i) => i.id === "u_prev" || i.id === "u_last")).toHaveLength(2);
  });

  it("stops at MAX_INITIAL_PAGES, leaving hasMore=true so scroll-up still reaches older items", async () => {
    // Pathological: a turn so long that 8 pages never reach a second user
    // prompt. The helper must bound its requests and hand the rest back to
    // loadMoreHistory (hasMore stays true) rather than fetch unbounded.
    for (let i = 0; i < 20; i++) {
      fetchMock.mockResolvedValueOnce(pageBody([asstWire(`p${i}`)], true));
    }

    const page = await fetchInitialHistoryWindow("conv_abc");

    // MAX_INITIAL_PAGES is 8; never more, even with pages still available.
    expect(fetchMock).toHaveBeenCalledTimes(8);
    expect(page.hasMore).toBe(true);
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
