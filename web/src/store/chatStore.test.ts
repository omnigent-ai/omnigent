// Vitest cases for the chat store.
//
// `switchTo` opens the session SSE stream and hydrates session
// metadata plus committed item history. We mock `fetch` at the global so we
// control:
//   - `GET  /v1/sessions/{id}/stream`  → empty SSE body (pump
//     terminates immediately, no blocks delivered)
//   - `GET  /v1/sessions/{id}`         → session metadata snapshot
//   - `GET  /v1/sessions/{id}/items`   → paginated committed items
//   - `POST /v1/sessions`              → new session JSON
//   - `GET  /v1/runners`               → one online runner
//   - `PATCH /v1/sessions/{id}`        → runner / effort binding
//   - `POST /v1/sessions/{id}/events`  → `{ queued }` ack
//
// Session metadata and item pages are shimmed through `seedSession`
// helpers so tests can model capped snapshots and full transcripts.

import type * as IdentityModule from "@/lib/identity";

import { type InfiniteData, QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation, ConversationsPage } from "@/hooks/useConversations";
import type {
  AnyBlock,
  ElicitationBlock,
  ErrorBlock,
  NativeToolBlock,
  TextDone,
  ToolGroup,
  UserMessageBlock,
} from "@/lib/blocks";
import type { ConversationItem } from "@/lib/conversationItems";
import { itemsToBlocks } from "@/lib/itemsToBlocks";
import { buildBubbles } from "@/lib/renderItems";
import { INITIAL_WINDOW_ITEMS, SESSION_HISTORY_PAGE_SIZE } from "@/lib/sessionsApi";
import { SSE_STALL_TIMEOUT_MS } from "@/lib/sse";
import { getCurrentAuthorId } from "@/lib/identity";
import { PRESENCE_IDLE_AFTER_MS } from "@/lib/presenceIdle";
import type {
  SessionCreatedEvent,
  SessionInputConsumedEvent,
  SessionInterruptedEvent,
  SessionResourceCreatedEvent,
  SessionResourceDeletedEvent,
  SessionStatusEvent,
  SessionTerminalPendingEvent,
  StreamEvent,
} from "@/lib/events";
import type { TerminalInfo } from "@/hooks/useTerminals";
import { terminalsQueryKey } from "@/hooks/useTerminals";
import { type ChildSessionInfo, childSessionsQueryKey } from "@/hooks/useChildSessions";
import {
  consumePendingInitialPrompt,
  handleSessionEvent,
  isStaleCompletedResponse,
  initChatStore,
  pumpStreamEvents,
  SSE_STALE_RECYCLE_MS,
  setPendingInitialPrompt,
  startStreamPump,
  useChatStore,
  type ConversationState,
  type FrameScheduler,
  bindConversationForTest,
  releaseConversation,
} from "./chatStore";
import { conversationRegistry } from "./conversationRegistry";
import {
  resetStreamSlotManager,
  setStreamSlotManagerForTest,
  type StreamSlot,
  type StreamSlotManager,
} from "./streamSlots";
import { useTerminalActivityStore } from "./terminalActivity";

// The real `send` action, captured before any test stubs it via
// setState({ send: spy }). Zustand's setState permanently overwrites the
// action, so beforeEach restores this so a later test (e.g. cross-path
// ordering) exercises the genuine send() path rather than a leftover spy.
const realSend = useChatStore.getState().send;

// Stub the viewer-identity lookup for deterministic author stamping on
// optimistic sends. Defaults to null — the same value the real module
// returns before identity resolves / in single-user mode — so tests
// don't pick up ambient identity. A `vi.fn` (not the real impl) so
// per-test `mockReturnValue` works; reset in afterEach so a stubbed
// identity never leaks across tests.
vi.mock("@/lib/identity", async (importOriginal) => {
  const actual = await importOriginal<typeof IdentityModule>();
  return { ...actual, getCurrentAuthorId: vi.fn<() => string | null>(() => null) };
});

function userMessage(responseId: string, text: string): ConversationItem {
  return {
    id: `msg_${responseId}_user`,
    response_id: responseId,
    type: "message",
    role: "user",
    status: "completed",
    content: [{ type: "input_text", text }],
  };
}

function assistantMessage(responseId: string, text: string): ConversationItem {
  return {
    id: `msg_${responseId}_asst`,
    response_id: responseId,
    type: "message",
    role: "assistant",
    status: "completed",
    model: "test-agent",
    content: [{ type: "output_text", text }],
  };
}

function nativeToolItem(responseId: string): ConversationItem {
  return {
    id: `nt_${responseId}`,
    response_id: responseId,
    type: "mcp_call",
    status: "completed",
    name: "read_database",
    data: { rows: 3 },
  };
}

// Controllers of every open `emptyStream`, closed in `afterEach`.
//
// Aborting the store's `AbortController` does NOT settle a parked
// `reader.read()`: `mockResponse` hands back a plain object, so there is no
// fetch plumbing tying the signal to the stream (a real fetch errors the body on
// abort). Without closing these explicitly, `parseSseStream` stays parked on the
// read forever and the pump outlives the test — `conversationRegistry.clear()`
// aborts the controller, which is not enough.
const openEmptyStreams = new Set<ReadableStreamDefaultController<Uint8Array>>();

// A stream that delivers no events and stays open, like a real idle session
// stream between turns (the server holds it and emits heartbeats). Never
// closing keeps it OPEN — `switchTo`/`ensureBoundSession` treat a stream that
// ended as no longer current and re-bind, so a mock that closed immediately
// would make every entry look dead and defeat background retention.
//
// Deliberately not `data: [DONE]`: in production that sentinel means a
// clean server-side close, which is terminal by design.
function emptyStream(): ReadableStream<Uint8Array> {
  let own: ReadableStreamDefaultController<Uint8Array> | null = null;
  return new ReadableStream({
    start(controller) {
      own = controller;
      openEmptyStreams.add(controller);
    },
    cancel() {
      // The consumer let go first, so this one needs no closing. Drop only its
      // own controller — the set holds every other live stream's too.
      if (own !== null) openEmptyStreams.delete(own);
    },
  });
}

/** Close every open `emptyStream` so parked readers settle. */
function closeOpenEmptyStreams(): void {
  for (const controller of openEmptyStreams) {
    try {
      controller.close();
    } catch {
      // Already closed or errored — nothing to do.
    }
  }
  openEmptyStreams.clear();
}

function mockResponse(
  body: unknown,
  init?: {
    ok?: boolean;
    status?: number;
    bodyStream?: ReadableStream<Uint8Array> | null;
    contentType?: string;
  },
): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: "OK",
    // Stream responses default to the SSE content type the pump's
    // non-SSE-answer guard expects; JSON handlers don't care.
    headers: new Headers({
      "content-type":
        init?.contentType ?? (init?.bodyStream ? "text/event-stream" : "application/json"),
    }),
    json: async () => body,
    text: async () => JSON.stringify(body),
    body: init?.bodyStream ?? null,
  } as unknown as Response;
}

function streamResponse(): Response {
  return mockResponse(null, { bodyStream: emptyStream() });
}

/** A byte stream the test pushes SSE frames into, then closes or errors. */
interface StreamSink {
  stream: ReadableStream<Uint8Array>;
  push: (frame: string) => void;
  close: () => void;
  error: (err?: unknown) => void;
}

function pushableStream(): StreamSink {
  let ctrl: ReadableStreamDefaultController<Uint8Array> | null = null;
  const enc = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      ctrl = c;
    },
  });
  return {
    stream,
    push: (frame) => ctrl!.enqueue(enc.encode(frame)),
    close: () => ctrl!.close(),
    error: (err) => ctrl!.error(err ?? new Error("stream dropped")),
  };
}

/** Serialize one SSE frame. */
function sse(event: string, data: Record<string, unknown>): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

let client: QueryClient;
const fetchMock = vi.fn();
let sessionSnapshots: Map<string, ConversationItem[]>;
let sessionItems: Map<string, ConversationItem[]>;
let sessionPendingElicitations: Map<string, Record<string, unknown>[]>;
let sessionPendingInputs: Map<
  string,
  { pending_id: string; content: unknown[]; created_by?: string }[]
>;
// Per-session cost-control switch the snapshot/PATCH handlers serve;
// absent key = unset (the wire field comes back null).
let sessionCostControlOverrides: Map<string, "on" | "off">;
let sessionSubagentRoutingOverrides: Map<string, "on" | "off">;
// Per-session labels the snapshot/PATCH handlers serve.
let sessionLabels: Map<string, Record<string, string>>;

/** Default fetch router: dispatch by URL. Tests override per-call as needed. */
function defaultFetchHandler(input: RequestInfo | URL, init?: RequestInit): Response {
  const url = typeof input === "string" ? input : input.toString();
  // Path without the query string: getSessionSlim appends
  // ?include_items=false&include_liveness=false, which must not leak into
  // the matched session id below.
  const path = url.split("?")[0]!;
  if (url.match(/\/v1\/sessions\/[^/]+\/stream$/)) return streamResponse();
  const itemsMatch = url.match(/^\/v1\/sessions\/([^/]+)\/items(?:\?.*)?$/);
  if (itemsMatch) {
    const sessionId = itemsMatch[1]!;
    const parsedUrl = new URL(url, "http://test.local");
    const limit = Number(parsedUrl.searchParams.get("limit") ?? "100");
    const order = parsedUrl.searchParams.get("order") ?? "asc";
    const after = parsedUrl.searchParams.get("after");
    const before = parsedUrl.searchParams.get("before");
    const allItems = sessionItems.get(sessionId) ?? sessionSnapshots.get(sessionId) ?? [];
    // Faithful copy of the server's position-cursor query: position ==
    // chronological index. after/before are relative to the sort
    // direction; the page is `limit` items after sorting + filtering.
    const posOf = (id: string) => allItems.findIndex((item) => item.id === id);
    let pool = allItems.map((item, pos) => ({ item, pos }));
    if (after !== null) {
      const a = posOf(after);
      pool = pool.filter((p) => (order === "asc" ? p.pos > a : p.pos < a));
    }
    if (before !== null) {
      const b = posOf(before);
      pool = pool.filter((p) => (order === "asc" ? p.pos < b : p.pos > b));
    }
    pool.sort((x, y) => (order === "asc" ? x.pos - y.pos : y.pos - x.pos));
    const data = pool.slice(0, limit).map((p) => p.item);
    return mockResponse({
      object: "list",
      data,
      first_id: data[0]?.id ?? null,
      last_id: data.at(-1)?.id ?? null,
      has_more: pool.length > limit,
    });
  }
  if (url.match(/\/v1\/sessions\/[^/]+\/events$/)) {
    return mockResponse({ queued: true, item_id: "ci_mock" });
  }
  // URL-based elicitation resolve endpoint (the approve button's
  // target). Returns the {queued: false} ack the server sends.
  if (url.match(/\/v1\/sessions\/[^/]+\/elicitations\/[^/]+\/resolve$/)) {
    return mockResponse({ queued: false });
  }
  if (url === "/v1/runners") {
    return mockResponse({
      data: [
        {
          runner_id: "runner_ui_test",
          online: true,
          harnesses: ["openai-agents"],
        },
      ],
    });
  }
  // GET /v1/sessions/{id} — snapshot used by bindStream for items + agentId.
  const snapshotMatch = path.match(/^\/v1\/sessions\/([^/]+)$/);
  if (snapshotMatch && init?.method === "PATCH") {
    const sessionId = snapshotMatch[1]!;
    const body = init.body
      ? (JSON.parse(init.body as string) as {
          runner_id?: string;
          cost_control_mode_override?: "on" | "off" | null;
          subagent_routing_override?: "on" | "off" | null;
          collaboration_mode?: string;
        })
      : {};
    const labels = { ...(sessionLabels.get(sessionId) ?? {}) };
    // Echo a cost-switch write back like the real server does, so the
    // store's canonical refresh sees the persisted value.
    if ("cost_control_mode_override" in body) {
      if (body.cost_control_mode_override == null) {
        sessionCostControlOverrides.delete(sessionId);
      } else {
        sessionCostControlOverrides.set(sessionId, body.cost_control_mode_override);
      }
    }
    if ("subagent_routing_override" in body) {
      if (body.subagent_routing_override == null) {
        sessionSubagentRoutingOverrides.delete(sessionId);
      } else {
        sessionSubagentRoutingOverrides.set(sessionId, body.subagent_routing_override);
      }
    }
    if ("collaboration_mode" in body && typeof body.collaboration_mode === "string") {
      labels["omnigent.codex_native.collaboration_mode"] = body.collaboration_mode;
      sessionLabels.set(sessionId, labels);
    }
    return mockResponse({
      id: sessionId,
      agent_id: "agent_xyz",
      runner_id: body.runner_id ?? null,
      status: "idle",
      created_at: 0,
      items: sessionSnapshots.get(sessionId) ?? [],
      labels,
      cost_control_mode_override: sessionCostControlOverrides.get(sessionId) ?? null,
      subagent_routing_override: sessionSubagentRoutingOverrides.get(sessionId) ?? null,
    });
  }
  if (snapshotMatch && (init?.method ?? "GET") === "GET") {
    const sessionId = snapshotMatch[1]!;
    return mockResponse({
      id: sessionId,
      agent_id: "agent_xyz",
      status: "idle",
      created_at: 0,
      items: sessionSnapshots.get(sessionId) ?? [],
      labels: sessionLabels.get(sessionId) ?? {},
      pending_elicitations: sessionPendingElicitations.get(sessionId) ?? [],
      pending_inputs: sessionPendingInputs.get(sessionId) ?? [],
      cost_control_mode_override: sessionCostControlOverrides.get(sessionId) ?? null,
      subagent_routing_override: sessionSubagentRoutingOverrides.get(sessionId) ?? null,
    });
  }
  if (url === "/v1/sessions" && init?.method === "POST") {
    const parsed = init?.body
      ? (JSON.parse(init.body as string) as { agent_id: string })
      : { agent_id: "?" };
    return mockResponse({
      id: "conv_new",
      agent_id: parsed.agent_id,
      status: "idle",
      created_at: 0,
      items: [],
    });
  }
  throw new Error(`Unhandled fetch in test: ${init?.method ?? "GET"} ${url}`);
}

/** Minimal sidebar Conversation row with a given status. */
function conv(id: string, status: Conversation["status"]): Conversation {
  return {
    id,
    object: "conversation",
    title: null,
    created_at: 0,
    updated_at: 0,
    labels: {},
    permission_level: null,
    status,
  };
}

/** Seed the sidebar conversations infinite-query cache (default "" search variant). */
function seedConversationsCache(convs: Conversation[]): void {
  client.setQueryData<InfiniteData<ConversationsPage>>(["conversations", ""], {
    pages: [
      {
        data: convs,
        first_id: convs[0]?.id ?? null,
        last_id: convs.at(-1)?.id ?? null,
        has_more: false,
      },
    ],
    pageParams: [undefined],
  });
}

/** Flatten the seeded conversations cache back to its rows. */
function readConversationRows(): Conversation[] {
  const data = client.getQueryData<InfiniteData<ConversationsPage>>(["conversations", ""]);
  return data?.pages.flatMap((p) => p.data) ?? [];
}

/**
 * Deterministic origin-wide slot manager for tests (jsdom has no
 * `navigator.locks`). `capacity` total slots, of which `externallyHeld` model
 * other tabs' holds; the rest are grantable to this tab.
 */
interface FakeSlotManager extends StreamSlotManager {
  heldByTab: () => number;
  setCapacity: (n: number) => void;
  setExternallyHeld: (n: number) => void;
}
function makeFakeSlotManager(
  opts: { capacity?: number; externallyHeld?: number } = {},
): FakeSlotManager {
  let capacity = opts.capacity ?? 100;
  let externallyHeld = opts.externallyHeld ?? 0;
  let held = 0;
  return {
    tryAcquire: (): Promise<StreamSlot | null> => {
      if (externallyHeld + held >= capacity) return Promise.resolve(null);
      held += 1;
      let released = false;
      return Promise.resolve({
        release: () => {
          if (!released) {
            released = true;
            held -= 1;
          }
          return Promise.resolve();
        },
      });
    },
    heldByTab: () => held,
    setCapacity: (n) => {
      capacity = n;
    },
    setExternallyHeld: (n) => {
      externallyHeld = n;
    },
  };
}

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  sessionSnapshots = new Map();
  sessionItems = new Map();
  sessionPendingElicitations = new Map();
  sessionPendingInputs = new Map();
  sessionCostControlOverrides = new Map();
  sessionSubagentRoutingOverrides = new Map();
  sessionLabels = new Map();
  initChatStore(client);
  // Generous, deterministic slots for tests that aren't about the cap; the
  // dedicated stream-slot tests install their own small-capacity manager.
  setStreamSlotManagerForTest(makeFakeSlotManager());
  useChatStore.setState({
    conversationId: null,
    blocks: [],
    pendingUserMessages: [],
    queuedMessages: [],
    activeResponse: null,
    status: "idle",
    sessionStatus: "idle",
    isNativeTerminalSession: false,
    loadingConversation: false,
    conversationLoadError: null,
    costControlModeOverride: null,
    subagentRoutingOverride: null,
    codexPlanMode: false,
    abortController: null,
    streamBudgetExceeded: false,
    streamBudgetBannerDismissed: false,
    // Restore the real send action; a prior test may have stubbed it.
    send: realSend,
  });
  fetchMock.mockReset();
  fetchMock.mockImplementation(defaultFetchHandler);
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  useChatStore.getState().abortController?.abort();
  // Every live entry owns an open stream now (the default mock never closes),
  // so tear the registry down or its pumps outlive the test and leak into the
  // next one's fetch mock.
  conversationRegistry.clear();
  // Aborting is not enough on its own: the mocked Response has no fetch
  // plumbing, so nothing errors the body and `parseSseStream` stays parked on
  // `reader.read()`. Close the streams so those reads actually settle.
  closeOpenEmptyStreams();
  resetStreamSlotManager();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  // Reset the stubbed viewer identity to the default (null) so a value set
  // in one test doesn't leak into the next.
  vi.mocked(getCurrentAuthorId).mockReturnValue(null);
});

/** Yield to the microtask queue so background pump kicks off. */
const tick = () =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, 0);
  });

/** Seed the session snapshot returned by GET /v1/sessions/{id}. */
function seedSession(id: string, items: ConversationItem[] = []): void {
  sessionSnapshots.set(id, items);
  sessionItems.set(id, items);
}

function seedSessionSnapshot(id: string, items: ConversationItem[] = []): void {
  sessionSnapshots.set(id, items);
}

function seedSessionItems(id: string, items: ConversationItem[] = []): void {
  sessionItems.set(id, items);
}

function seedPendingElicitations(id: string, events: Record<string, unknown>[]): void {
  sessionPendingElicitations.set(id, events);
}

function seedPendingInputs(
  id: string,
  inputs: { pending_id: string; content: unknown[]; created_by?: string }[],
): void {
  sessionPendingInputs.set(id, inputs);
}

describe("test harness teardown", () => {
  it("settles a parked SSE reader, which aborting alone cannot do", async () => {
    // Guards the `afterEach` contract. The default `/stream` mock stays open (so
    // background retention is exercised faithfully), but `mockResponse` returns a
    // plain object with no fetch plumbing — so aborting the store's controller
    // never errors the body, and `parseSseStream` would stay parked on
    // `reader.read()` for the whole run. Closing the stream is what settles it.
    seedSession("conv_teardown", []);
    await useChatStore.getState().switchTo("conv_teardown");
    expect(openEmptyStreams.size).toBeGreaterThan(0);

    // What `conversationRegistry.clear()` does, and all it does.
    useChatStore.getState().abortController?.abort();
    const stream = emptyStream();
    const reader = stream.getReader();
    let settled = false;
    void reader.read().then(() => {
      settled = true;
    });
    await tick();
    // Still parked: the abort above has no relationship to this stream.
    expect(settled).toBe(false);

    closeOpenEmptyStreams();
    await tick();
    // Closing settled it, and the registry of open streams is empty.
    expect(settled).toBe(true);
    expect(openEmptyStreams.size).toBe(0);
  });
});

describe("chatStore — switchTo", () => {
  it("hydrates blocks from the session snapshot when switching to a real conv id", async () => {
    const items: ConversationItem[] = [
      userMessage("resp_1", "hello"),
      assistantMessage("resp_1", "hi there"),
    ];
    seedSession("conv_abc", items);

    await useChatStore.getState().switchTo("conv_abc");

    const state = useChatStore.getState();
    expect(state.conversationId).toBe("conv_abc");
    expect(state.blocks.length).toBe(2);
    expect(state.blocks[0]!.type).toBe("user_message");
    expect(state.blocks[1]!.type).toBe("text_done");
    const user = state.blocks[0] as UserMessageBlock;
    expect(user.content).toEqual([{ type: "input_text", text: "hello" }]);
    expect(state.loadingConversation).toBe(false);
    expect(state.conversationLoadError).toBeNull();
  });

  it("keeps a backgrounded conversation live, so returning to it needs no refetch", async () => {
    // The feature: switching away no longer tears the stream down, so switching
    // back neither re-opens a stream nor re-fetches the snapshot. This is what
    // the transcript LRU used to approximate (paint stale, then revalidate).
    seedSession("conv_a", [userMessage("resp_a", "in a")]);
    seedSession("conv_b", [userMessage("resp_b", "in b")]);

    await useChatStore.getState().switchTo("conv_a");
    expect(useChatStore.getState().blocks).toHaveLength(1);

    await useChatStore.getState().switchTo("conv_b");
    const callsAfterB = fetchMock.mock.calls.length;

    await useChatStore.getState().switchTo("conv_a");
    // Painted from the live entry: conv_a's transcript is right there.
    expect(useChatStore.getState().conversationId).toBe("conv_a");
    expect(useChatStore.getState().blocks).toHaveLength(1);
    // And nothing was fetched to get it.
    expect(fetchMock.mock.calls.length).toBe(callsAfterB);
  });

  it("commits a message sent in a backgrounded conversation, in transcript order", async () => {
    // End-to-end version of the interleaving regression, through the real pump.
    //
    // The renderer appends `pendingUserMessages` AFTER committed `blocks`
    // (ChatPage's `mergePendingBubbles`), so an optimistic bubble is always
    // visually last. That is correct while it is in flight, and becomes a bug if
    // it never commits: `session.input.consumed` is the ONLY thing that promotes
    // it into `blocks`. Drop that event for a background conversation and the
    // user message stays pinned below assistant output that arrived after it —
    // which reads as scrambled ordering when the user switches back.
    const sink = pushableStream();
    seedSession("conv_bgsend", []);
    seedSession("conv_other", []);
    const base = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/v1/sessions/conv_bgsend/stream")
        return mockResponse(null, { bodyStream: sink.stream });
      return base(input, init);
    });

    await useChatStore.getState().switchTo("conv_bgsend");
    await useChatStore.getState().send("hello from bg", "agent_xyz");
    // Optimistic bubble is pending: nothing committed yet.
    expect(useChatStore.getState().pendingUserMessages).toHaveLength(1);

    // The user navigates away; conv_bgsend keeps its stream.
    await useChatStore.getState().switchTo("conv_other");

    // The runner commits the message, then streams its reply — the order the
    // user should see on return.
    sink.push(
      sse("session.input.consumed", {
        // Nested envelope, as the server sends it (see `parseEvent`).
        data: {
          item_id: "item_bg_user",
          type: "message",
          data: { role: "user", content: [{ type: "input_text", text: "hello from bg" }] },
        },
      }),
    );
    sink.push(sse("response.created", { id: "resp_bg", status: "in_progress", output: [] }));
    // Past the reducer's flush threshold so the text lands as a block.
    sink.push(sse("response.output_text.delta", { delta: `${"z".repeat(34)} ` }));
    await tick();
    await tick();

    // Back to the backgrounded conversation.
    await useChatStore.getState().switchTo("conv_bgsend");
    const state = useChatStore.getState();

    // The bubble was promoted, so nothing trails the transcript...
    expect(state.pendingUserMessages).toEqual([]);
    // ...and the user message sits BEFORE the assistant reply that followed it.
    const kinds = state.blocks.map((b) => b.type);
    expect(kinds[0]).toBe("user_message");
    expect(state.blocks[0]!.ctx.itemId).toBe("item_bg_user");
    expect(kinds.slice(1)).not.toContain("user_message");

    sink.close();
  });

  it("re-binds a retained conversation whose stream ended, instead of painting it stale", async () => {
    // Registry membership alone does not mean "current". A clean server close
    // (`[DONE]`) ends the pump for good and clears `abortController`, but does
    // not release the entry — so treating "has an entry" as live meant returning
    // to the conversation repainted whatever it held when the stream died and
    // never reopened it: permanently stale until the next send.
    const sink = pushableStream();
    seedSession("conv_dead", [userMessage("resp_d", "first")]);
    seedSession("conv_away", []);
    const base = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/v1/sessions/conv_dead/stream")
        return mockResponse(null, { bodyStream: sink.stream });
      return base(input, init);
    });

    await useChatStore.getState().switchTo("conv_dead");
    expect(useChatStore.getState().blocks).toHaveLength(1);

    // The server closes the stream deliberately; the pump stops for good.
    sink.push("data: [DONE]\n\n");
    sink.close();
    await tick();
    await tick();
    expect(conversationRegistry.peek("conv_dead")!.getState().abortController).toBeNull();

    await useChatStore.getState().switchTo("conv_away");
    // Meanwhile the conversation gained an item the dead stream never delivered.
    seedSession("conv_dead", [
      userMessage("resp_d", "first"),
      assistantMessage("resp_d", "second"),
    ]);

    await useChatStore.getState().switchTo("conv_dead");
    // Re-bound: the fresh snapshot is here, exactly once.
    expect(useChatStore.getState().blocks).toHaveLength(2);
    expect(useChatStore.getState().blocks.map((b) => b.type)).toEqual([
      "user_message",
      "text_done",
    ]);
    expect(useChatStore.getState().abortController).not.toBeNull();
  });

  it("keeps an unsent bubble when re-binding a conversation whose stream died", async () => {
    // The re-bind starts from a clean entry so the snapshot can't duplicate a
    // stale transcript — but a send the server never acknowledged exists nowhere
    // else, so dropping the entry wholesale would lose the user's message.
    seedSession("conv_rebind_keep", []);
    await useChatStore.getState().switchTo("conv_rebind_keep");
    const entry = conversationRegistry.peek("conv_rebind_keep")!;
    // A dead stream (cleared controller) holding an unsettled optimistic bubble.
    entry.setState({
      abortController: null,
      pendingUserMessages: [{ tempId: "pend_keep", content: [{ type: "input_text", text: "hi" }] }],
    });
    await useChatStore.getState().switchTo(null);

    await useChatStore.getState().switchTo("conv_rebind_keep");

    expect(useChatStore.getState().pendingUserMessages.map((p) => p.tempId)).toEqual(["pend_keep"]);
  });

  it("reconciles a backgrounded conversation's stale approval card on tab focus", async () => {
    // `bindStream` registers a visibilitychange reconcile per stream, so every
    // LIVE conversation gets one — not just the visible one. Gating the apply on
    // the active conversation made a background reconcile fetch the snapshot and
    // then discard it, leaving a card that was answered elsewhere stuck pending
    // until a page refresh.
    seedSession("conv_vis_elic", []);
    seedPendingElicitations("conv_vis_elic", [
      {
        type: "response.elicitation_request",
        elicitation_id: "elic_vis",
        params: {
          mode: "form",
          message: "Approve while hidden?",
          phase: "tool_call_approval",
          policy_name: "test_policy",
          content_preview: "",
        },
      },
    ]);
    seedSession("conv_vis_other", []);

    await useChatStore.getState().switchTo("conv_vis_elic");
    const cardOf = (id: string) =>
      conversationRegistry
        .peek(id)!
        .getState()
        .blocks.filter((b): b is ElicitationBlock => b.type === "elicitation");
    expect(cardOf("conv_vis_elic")[0]!.status).toBe("pending");

    // Background it, then the prompt is answered on another surface.
    await useChatStore.getState().switchTo("conv_vis_other");
    seedPendingElicitations("conv_vis_elic", []);

    // Tab regains focus: every live stream's listener reconciles.
    document.dispatchEvent(new Event("visibilitychange"));
    await tick();
    await tick();

    const reconciled = cardOf("conv_vis_elic");
    expect(reconciled).toHaveLength(1);
    expect(reconciled[0]!.status).toBe("responded");
    expect(reconciled[0]!.response).toEqual({ action: "auto_resolved" });
  });

  it("does not abort a conversation's stream when switching away", async () => {
    const sink = pushableStream();
    seedSession("conv_a", []);
    seedSession("conv_b", []);
    const base = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/v1/sessions/conv_a/stream")
        return mockResponse(null, { bodyStream: sink.stream });
      return base(input, init);
    });

    await useChatStore.getState().switchTo("conv_a");
    const aController = useChatStore.getState().abortController;
    expect(aController).not.toBeNull();

    await useChatStore.getState().switchTo("conv_b");
    // conv_a keeps pumping in the background — that is the whole point.
    expect(aController!.signal.aborted).toBe(false);

    sink.close();
  });

  it("keeps each conversation's transcript separate across switches", async () => {
    seedSession("conv_a", [userMessage("resp_a", "in a")]);
    seedSession("conv_b", [userMessage("resp_b", "in b"), assistantMessage("resp_b", "reply b")]);

    await useChatStore.getState().switchTo("conv_a");
    await useChatStore.getState().switchTo("conv_b");
    expect(useChatStore.getState().blocks).toHaveLength(2);
    await useChatStore.getState().switchTo("conv_a");
    expect(useChatStore.getState().blocks).toHaveLength(1);
  });

  it("hydrates pendingUserMessages from the snapshot's pending_inputs (native rebind)", async () => {
    // The core fix: a native web message that hasn't round-tripped
    // through the transcript yet is replayed by the server in
    // pending_inputs. switchTo (which resets pendingUserMessages) must
    // re-hydrate the optimistic bubble from it — otherwise navigating
    // away and back loses the message until it persists.
    seedSession("conv_native", []);
    seedPendingInputs("conv_native", [
      { pending_id: "pending_srv1", content: [{ type: "input_text", text: "queued msg" }] },
    ]);

    await useChatStore.getState().switchTo("conv_native");

    const state = useChatStore.getState();
    // Keyed by the server pending id so a later consumed event drops it
    // by id, and content is replayed verbatim so the bubble renders.
    expect(state.pendingUserMessages).toEqual([
      { tempId: "pending_srv1", content: [{ type: "input_text", text: "queued msg" }] },
    ]);
  });

  it("drops a snapshot pending_input whose message already committed (no resume re-inject)", async () => {
    // Regression: a native pending_inputs entry whose message DID
    // round-trip into history (or a ghost the TUI never accepted that
    // lingers until its server-side TTL) must NOT replay as a second
    // bubble on a cold-load resume — that is the "initial prompt got
    // re-injected on resume" report. The committed item is the single
    // source of truth; the stale pending copy is dropped.
    seedSession("conv_native", [userMessage("resp1", "the initial prompt")]);
    seedPendingInputs("conv_native", [
      {
        pending_id: "pending_stale",
        content: [{ type: "input_text", text: "the initial prompt" }],
      },
      {
        pending_id: "pending_live",
        content: [{ type: "input_text", text: "a genuinely queued msg" }],
      },
    ]);

    await useChatStore.getState().switchTo("conv_native");

    // Only the not-yet-committed entry survives; the one already in
    // history is dropped so the message renders exactly once.
    expect(useChatStore.getState().pendingUserMessages).toEqual([
      { tempId: "pending_live", content: [{ type: "input_text", text: "a genuinely queued msg" }] },
    ]);
  });

  it("keeps a pending_input matching a committed message that the transcript reformatted", async () => {
    // The native transcript can prepend markers/blockquotes, so the
    // committed text is a superset of the POSTed text. Containment (not
    // equality) still dedupes it, so the optimistic bubble doesn't
    // double up with the reformatted committed copy.
    seedSession("conv_native", [userMessage("resp1", "[Attached: a.png]\n\nthe initial prompt")]);
    seedPendingInputs("conv_native", [
      {
        pending_id: "pending_stale",
        content: [{ type: "input_text", text: "the initial prompt" }],
      },
    ]);

    await useChatStore.getState().switchTo("conv_native");

    expect(useChatStore.getState().pendingUserMessages).toEqual([]);
  });

  it("keeps an image-only pending_input that has no text to dedupe on", async () => {
    // An image-only message carries no text, so it can't be matched
    // against committed history. It must be kept — dropping it would
    // lose the user's upload bubble on resume.
    seedSession("conv_native", [userMessage("resp1", "some committed text")]);
    seedPendingInputs("conv_native", [
      {
        pending_id: "pending_img",
        content: [{ type: "input_image", file_id: "file_xyz", filename: "a.png" }],
      },
    ]);

    await useChatStore.getState().switchTo("conv_native");

    expect(useChatStore.getState().pendingUserMessages).toEqual([
      {
        tempId: "pending_img",
        content: [{ type: "input_image", file_id: "file_xyz", filename: "a.png" }],
      },
    ]);
  });

  // The session.status handler keys off isNativeTerminalSession to decide
  // whether to clear the optimistic bubble on idle (see that handler's
  // test). It must be true for every registered native wrapper and false otherwise,
  // or the host-restart "bubble disappears" fix mis-fires.
  // ``isNativeTerminalSession`` gates the optimistic-bubble clear; the companion
  // ``nativeVendorOwnsModel`` hides the composer model/effort chip for native
  // wrappers whose model is chosen inside the vendor TUI (qwen/goose/pi/cursor/
  // opencode) — claude/codex keep it (they expose an Omnigent model picker).
  it.each([
    ["claude-code-native-ui", true, false],
    ["codex-native-ui", true, false],
    ["pi-native-ui", true, true],
    ["qwen-native-ui", true, true],
    ["goose-native-ui", true, true],
    ["some-other-wrapper", false, false],
    [null, false, false],
  ])(
    "switchTo derives native flags from wrapper=%s",
    async (wrapper, expectedNative, expectedVendorOwnsModel) => {
      seedSession("conv_wrap", []);
      fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.toString();
        if (url.split("?")[0] === "/v1/sessions/conv_wrap" && (init?.method ?? "GET") === "GET") {
          return mockResponse({
            id: "conv_wrap",
            agent_id: "agent_xyz",
            status: "idle",
            created_at: 0,
            items: [],
            labels: wrapper === null ? {} : { "omnigent.wrapper": wrapper },
          });
        }
        return defaultFetchHandler(input, init);
      });

      await useChatStore.getState().switchTo("conv_wrap");

      expect(useChatStore.getState().isNativeTerminalSession).toBe(expectedNative);
      expect(useChatStore.getState().nativeVendorOwnsModel).toBe(expectedVendorOwnsModel);
    },
  );

  it("refetches the session snapshot even when a stale cached session exists", async () => {
    client.setQueryData(["session", "conv_abc"], {
      id: "conv_abc",
      agentId: "agent_xyz",
      status: "idle",
      createdAt: 0,
      items: [userMessage("resp_old", "stale cached message")],
    });
    seedSession("conv_abc", [userMessage("resp_new", "fresh server message")]);

    await useChatStore.getState().switchTo("conv_abc");

    const sessionFetches = fetchMock.mock.calls.filter(
      ([u, init]) =>
        String(u).split("?")[0] === "/v1/sessions/conv_abc" && (init?.method ?? "GET") === "GET",
    );
    // One GET proves bindStream refetched instead of trusting the
    // pre-populated React Query session cache.
    expect(sessionFetches).toHaveLength(1);
    expect(String(sessionFetches[0]?.[0])).toContain("refresh_state=true");

    const blocks = useChatStore.getState().blocks;
    // The server snapshot has one message; seeing any other count would
    // mean stale cached data was reused or hydration duplicated blocks.
    expect(blocks).toHaveLength(1);
    expect((blocks[0] as UserMessageBlock).content).toEqual([
      { type: "input_text", text: "fresh server message" },
    ]);
  });

  it("hydrates from paginated session items instead of the capped session snapshot", async () => {
    const fullItems: ConversationItem[] = [
      userMessage("resp_1", "first user"),
      assistantMessage("resp_1", "first assistant"),
      userMessage("resp_2", "second user"),
      assistantMessage("resp_2", "second assistant"),
    ];
    seedSessionSnapshot("conv_long", [assistantMessage("resp_2", "snapshot tail only")]);
    seedSessionItems("conv_long", fullItems);

    await useChatStore.getState().switchTo("conv_long");

    const blocks = useChatStore.getState().blocks;
    expect(blocks).toHaveLength(4);
    expect((blocks[0] as UserMessageBlock).content).toEqual([
      { type: "input_text", text: "first user" },
    ]);
    expect(blocks[1]).toMatchObject({ type: "text_done", fullText: "first assistant" });
    expect((blocks[2] as UserMessageBlock).content).toEqual([
      { type: "input_text", text: "second user" },
    ]);
    expect(blocks[3]).toMatchObject({ type: "text_done", fullText: "second assistant" });
    expect(useChatStore.getState().hasMoreHistory).toBe(false);
  });

  it("hydrates pending elicitations from the session snapshot after refresh", async () => {
    seedSession("conv_waiting", [userMessage("resp_1", "please do risky thing")]);
    seedPendingElicitations("conv_waiting", [
      {
        type: "response.elicitation_request",
        elicitation_id: "elicit_waiting",
        params: {
          mode: "form",
          message: "Codex wants to run **date**",
          requestedSchema: null,
          phase: "codex_command_approval",
          policy_name: "codex_native_command_approval",
          content_preview: "date",
          command: "date",
          cwd: "/tmp/workspace",
          reason: "needs current time",
          target_session_id: "conv_child_waiting",
        },
      },
    ]);

    await useChatStore.getState().switchTo("conv_waiting");

    const blocks = useChatStore.getState().blocks;
    const elicitation = blocks.find(
      (b): b is ElicitationBlock =>
        b.type === "elicitation" && b.elicitationId === "elicit_waiting",
    );
    expect(elicitation).toBeDefined();
    expect(elicitation?.status).toBe("pending");
    expect(elicitation?.targetSessionId).toBe("conv_child_waiting");
    expect(elicitation?.message).toBe("Codex wants to run **date**");
    expect(elicitation?.codexCommand).toEqual({
      command: "date",
      cwd: "/tmp/workspace",
      reason: "needs current time",
      execPolicyAmendment: null,
    });

    await useChatStore.getState().submitApproval("elicit_waiting", "accept");

    const childCalls = fetchMock.mock.calls.filter(([u]) =>
      String(u).endsWith("/v1/sessions/conv_child_waiting/elicitations/elicit_waiting/resolve"),
    );
    const parentCalls = fetchMock.mock.calls.filter(([u]) =>
      String(u).endsWith("/v1/sessions/conv_waiting/elicitations/elicit_waiting/resolve"),
    );
    expect(childCalls).toHaveLength(1);
    expect(parentCalls).toHaveLength(0);
  });

  it("hydrates pending elicitations even when the stream response stalls", async () => {
    seedSession("conv_waiting", [userMessage("resp_1", "please do risky thing")]);
    seedPendingElicitations("conv_waiting", [
      {
        type: "response.elicitation_request",
        elicitation_id: "elicit_waiting",
        params: {
          mode: "form",
          message: "Approve stalled stream command?",
          requestedSchema: null,
          phase: "codex_command_approval",
          policy_name: "codex_native_command_approval",
          content_preview: "date",
          command: "date",
          cwd: "/tmp/workspace",
          reason: "needs current time",
        },
      },
    ]);
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.match(/\/v1\/sessions\/[^/]+\/stream$/)) {
        return new Promise<Response>((_, reject) => {
          init?.signal?.addEventListener(
            "abort",
            () => reject(new DOMException("aborted", "AbortError")),
            { once: true },
          );
        });
      }
      return defaultFetchHandler(input, init);
    });

    await Promise.race([
      useChatStore.getState().switchTo("conv_waiting"),
      new Promise<never>((_, reject) => {
        setTimeout(() => reject(new Error("switchTo timed out waiting for stalled stream")), 200);
      }),
    ]);

    const elicitation = useChatStore
      .getState()
      .blocks.find(
        (b): b is ElicitationBlock =>
          b.type === "elicitation" && b.elicitationId === "elicit_waiting",
      );
    expect(elicitation?.status).toBe("pending");
    expect(elicitation?.message).toBe("Approve stalled stream command?");
  });

  it("replaces an answered question card with the copy hydration rebuilt", async () => {
    // The user answered a question live; the tool call it gated then
    // persisted, so a rebind's hydration rebuilds the same card from the
    // items. Both copies rendering would show the exchange twice — and
    // they can't pair on ids, since the live elicitation id is never
    // persisted.
    const questions = {
      questions: [
        { question: "Which library?", options: [{ label: "date-fns" }], multiSelect: false },
      ],
    };
    seedSession("conv_answered", [userMessage("resp_1", "pick one")]);
    await useChatStore.getState().switchTo("conv_answered");
    useChatStore.setState((s) => ({
      blocks: [
        ...s.blocks,
        {
          type: "elicitation",
          ctx: { agent: null, depth: 0, turn: 0, timestamp: 0, responseId: "resp_1", itemId: null },
          elicitationId: "elicit_claude_native_deadbeef",
          message: "Claude wants to call **AskUserQuestion**",
          phase: "pre_tool_use",
          policyName: "claude_native_permission",
          contentPreview: "",
          requestedSchema: {},
          status: "responded",
          response: { action: "accept", content: { "Which library?": "date-fns" } },
          askUserQuestion: questions,
        } satisfies ElicitationBlock,
      ],
    }));
    seedSession("conv_answered", [
      userMessage("resp_1", "pick one"),
      {
        id: "fc_ask",
        response_id: "resp_1",
        type: "function_call",
        status: "completed",
        name: "AskUserQuestion",
        arguments: JSON.stringify(questions),
        call_id: "call_ask",
      },
      {
        id: "fco_ask",
        response_id: "resp_1",
        type: "function_call_output",
        status: "completed",
        call_id: "call_ask",
        output: 'Your questions have been answered: "Which library?"="date-fns".',
      },
    ]);

    // The stream died (idle disconnect): the send-triggered rebind
    // re-hydrates the window alongside the live blocks.
    useChatStore.setState({ abortController: null });
    await useChatStore.getState().send("thanks", "agent_xyz");

    const cards = useChatStore
      .getState()
      .blocks.filter((b): b is ElicitationBlock => b.type === "elicitation");
    expect(cards).toHaveLength(1);
    expect(cards[0]!.ctx.itemId).toBe("fc_ask:answer");
  });

  it("hydrates only the initial window and flags that older history remains", async () => {
    // Longer than the initial window so the loaded window is a strict subset.
    const total = INITIAL_WINDOW_ITEMS + SESSION_HISTORY_PAGE_SIZE + 5;
    const fullItems = Array.from({ length: total }, (_, idx) =>
      userMessage(`resp_${idx.toString().padStart(4, "0")}`, `message ${idx}`),
    );
    seedSessionSnapshot("conv_big", fullItems.slice(-INITIAL_WINDOW_ITEMS));
    seedSessionItems("conv_big", fullItems);

    await useChatStore.getState().switchTo("conv_big");

    const state = useChatStore.getState();
    // Only the initial window is hydrated. If bind regressed to loading the
    // whole transcript this would be `total` — the slow-open bug this
    // windowing fixes.
    expect(state.blocks).toHaveLength(INITIAL_WINDOW_ITEMS);
    // Newest item is the last block: proves we fetched the tail
    // (order=desc) and reversed to chronological, not the convo head.
    expect(state.blocks.at(-1)).toMatchObject({
      type: "user_message",
      ctx: { itemId: `msg_resp_${(total - 1).toString().padStart(4, "0")}_user` },
    });
    // Older items remain, so scroll-up loading is armed. `false` here
    // would strand the user with no way to reach earlier turns.
    expect(state.hasMoreHistory).toBe(true);
    // Exactly ONE request on bind. More than one means the open resumed
    // fetching history the reader never asked for.
    const itemFetches = fetchMock.mock.calls.filter(([u]) =>
      String(u).startsWith("/v1/sessions/conv_big/items"),
    );
    expect(itemFetches).toHaveLength(1);
    expect(String(itemFetches[0]![0])).toContain("order=desc");
  });

  it("loadMoreHistory prepends the page of items immediately older than the window", async () => {
    const total = INITIAL_WINDOW_ITEMS + SESSION_HISTORY_PAGE_SIZE + 5;
    const fullItems = Array.from({ length: total }, (_, idx) =>
      userMessage(`resp_${idx.toString().padStart(4, "0")}`, `message ${idx}`),
    );
    seedSessionSnapshot("conv_big", fullItems.slice(-INITIAL_WINDOW_ITEMS));
    seedSessionItems("conv_big", fullItems);

    await useChatStore.getState().switchTo("conv_big");
    // Precondition: bind loaded exactly the initial window.
    expect(useChatStore.getState().blocks).toHaveLength(INITIAL_WINDOW_ITEMS);

    await useChatStore.getState().loadMoreHistory();

    const state = useChatStore.getState();
    // Window + one older page. A wrong older-page cursor — e.g. the pre-fix
    // bug that fetched the convo head — would yield the wrong count or
    // duplicate blocks.
    expect(state.blocks).toHaveLength(INITIAL_WINDOW_ITEMS + SESSION_HISTORY_PAGE_SIZE);
    // The older page sits before the original, oldest item first. Its first
    // block is index `total - WINDOW - PAGE` (= 5): proves the prepend order
    // and the descending+after cursor math are correct.
    expect(state.blocks[0]).toMatchObject({
      type: "user_message",
      ctx: {
        itemId: `msg_resp_${(total - INITIAL_WINDOW_ITEMS - SESSION_HISTORY_PAGE_SIZE)
          .toString()
          .padStart(4, "0")}_user`,
      },
    });
    // 5 still-older items remain, so more is flagged.
    expect(state.hasMoreHistory).toBe(true);
  });

  it("applies a scroll-up page that resolves after navigating away and back", async () => {
    // This used to be "drops a stale page": navigating away destroyed the window
    // and re-hydrated it on return, so an in-flight cursor-relative page was
    // relative to a window that no longer existed. The conversation now keeps its
    // window across navigation, so the page is still valid when it lands and must
    // be applied rather than discarded.
    const total = INITIAL_WINDOW_ITEMS + SESSION_HISTORY_PAGE_SIZE;
    const itemsA = Array.from({ length: total }, (_, idx) =>
      userMessage(`a_${idx.toString().padStart(4, "0")}`, `a ${idx}`),
    );
    seedSession("conv_a", itemsA);
    seedSession("conv_b", []);

    await useChatStore.getState().switchTo("conv_a");
    const windowIds = useChatStore.getState().blocks.map((b) => b.ctx.itemId);
    expect(windowIds).toHaveLength(INITIAL_WINDOW_ITEMS);

    // Hold the scroll-up page open across the navigation round trip.
    let releasePage: (() => void) | null = null;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (/\/v1\/sessions\/conv_a\/items\?.*after=/.test(url) && releasePage === null) {
        return new Promise<Response>((resolve) => {
          releasePage = () => resolve(defaultFetchHandler(input, init));
        });
      }
      return defaultFetchHandler(input, init);
    });

    const inFlight = useChatStore.getState().loadMoreHistory();
    await useChatStore.getState().switchTo("conv_b");
    await useChatStore.getState().switchTo("conv_a");
    releasePage!();
    await inFlight;

    const state = useChatStore.getState();
    // Prepended onto the window it was fetched against, which is still current.
    expect(state.blocks.map((b) => b.ctx.itemId)).toEqual([
      ...itemsA.slice(0, SESSION_HISTORY_PAGE_SIZE).map((item) => item.id),
      ...windowIds,
    ]);
    expect(state.oldestItemId).toBe(itemsA[0]!.id);
    expect(state.hasMoreHistory).toBe(false);
    expect(state.loadingMoreHistory).toBe(false);
  });

  it("loadMoreHistory dedupes a page overlapping blocks kept across a rebind", async () => {
    const total = INITIAL_WINDOW_ITEMS + SESSION_HISTORY_PAGE_SIZE * 2;
    const items = Array.from({ length: total }, (_, idx) =>
      userMessage(`r_${idx.toString().padStart(4, "0")}`, `r ${idx}`),
    );
    seedSession("conv_re", items);

    await useChatStore.getState().switchTo("conv_re");
    await useChatStore.getState().loadMoreHistory();
    expect(useChatStore.getState().blocks).toHaveLength(
      INITIAL_WINDOW_ITEMS + SESSION_HISTORY_PAGE_SIZE,
    );

    // The stream died (idle disconnect): the send-triggered rebind
    // re-hydrates the newest window, resetting the cursor to its top while
    // the scrolled-up blocks stay rendered.
    useChatStore.setState({ abortController: null });
    await useChatStore.getState().send("hello again", "agent_xyz");
    expect(useChatStore.getState().oldestItemId).toBe(items.at(-INITIAL_WINDOW_ITEMS)!.id);

    // This page (older than the rewound cursor) fully overlaps the blocks
    // kept across the rebind — without itemId dedupe each would render twice.
    await useChatStore.getState().loadMoreHistory();
    expect(useChatStore.getState().blocks.map((b) => b.ctx.itemId)).toEqual(
      items.slice(SESSION_HISTORY_PAGE_SIZE).map((item) => item.id),
    );

    // The cursor advanced through the overlap, so the oldest page loads next.
    await useChatStore.getState().loadMoreHistory();
    expect(useChatStore.getState().blocks.map((b) => b.ctx.itemId)).toEqual(
      items.map((item) => item.id),
    );
  });

  it("clears loadingMoreHistory when a rebind hydration voids the in-flight page", async () => {
    const total = INITIAL_WINDOW_ITEMS + SESSION_HISTORY_PAGE_SIZE;
    const items = Array.from({ length: total }, (_, idx) =>
      userMessage(`lh_${idx.toString().padStart(4, "0")}`, `lh ${idx}`),
    );
    seedSession("conv_lh", items);

    await useChatStore.getState().switchTo("conv_lh");
    const windowIds = useChatStore.getState().blocks.map((b) => b.ctx.itemId);

    // Defer ONLY the scroll-up page; the rebind's cursorless fetches stay live.
    let releaseStalePage: (() => void) | null = null;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (/\/v1\/sessions\/conv_lh\/items\?.*after=/.test(url) && releaseStalePage === null) {
        return new Promise<Response>((resolve) => {
          releaseStalePage = () => resolve(defaultFetchHandler(input, init));
        });
      }
      return defaultFetchHandler(input, init);
    });

    const stalePage = useChatStore.getState().loadMoreHistory();
    expect(useChatStore.getState().loadingMoreHistory).toBe(true);

    // The stream died on idle; the send-triggered rebind hydration voids
    // the in-flight page (generation bump) while it is still unresolved.
    useChatStore.setState({ abortController: null });
    await useChatStore.getState().send("hello again", "agent_xyz");
    releaseStalePage!();
    await stalePage;

    // The voided page's stale early-return skips its own flag clear, so the
    // hydration must clear it — stuck true makes every future loadMoreHistory
    // no-op on the loadingMoreHistory guard (scroll-up dead permanently).
    expect(useChatStore.getState().loadingMoreHistory).toBe(false);

    // Scroll-up still works: the older page actually fetches and prepends.
    await useChatStore.getState().loadMoreHistory();
    expect(useChatStore.getState().blocks.map((b) => b.ctx.itemId)).toEqual([
      ...items.slice(0, SESSION_HISTORY_PAGE_SIZE).map((item) => item.id),
      ...windowIds,
    ]);
  });

  it("keeps each conversation's own effort across a warm A→B→A switch", async () => {
    // `selectedEffort` is a single app-global sticky pick, so it cannot answer
    // "what is THIS conversation at" once a warm switch skips hydration: cold
    // opening B set it to B's value, and returning to still-live A mirrored no
    // effort field and returned without a bind — so A displayed B's effort.
    // `sessionReasoningEffort` is conversation-scoped, so each keeps its own.
    seedSession("conv_eff_a", []);
    seedSession("conv_eff_b", []);
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const path = url.split("?")[0];
      if (
        (path === "/v1/sessions/conv_eff_a" || path === "/v1/sessions/conv_eff_b") &&
        (init?.method ?? "GET") === "GET"
      ) {
        return mockResponse({
          id: path.split("/").pop(),
          agent_id: "agent_xyz",
          status: "idle",
          created_at: 0,
          items: [],
          labels: { "omnigent.wrapper": "claude-code-native-ui" },
          reasoning_effort: path.endsWith("conv_eff_a") ? "high" : "low",
        });
      }
      return defaultFetchHandler(input, init);
    });

    await useChatStore.getState().switchTo("conv_eff_a");
    expect(useChatStore.getState().sessionReasoningEffort).toBe("high");

    // Cold-open B at a different effort. This moves the app-global sticky pick.
    await useChatStore.getState().switchTo("conv_eff_b");
    expect(useChatStore.getState().sessionReasoningEffort).toBe("low");

    // Warm switch back: no bind, so the projection is the only source of truth.
    await useChatStore.getState().switchTo("conv_eff_a");
    expect(useChatStore.getState().sessionReasoningEffort).toBe("high");
    // And B still holds its own.
    expect(conversationRegistry.peek("conv_eff_b")!.getState().sessionReasoningEffort).toBe("low");
  });

  it("loadMoreHistory settles on the conversation that scrolled, not the visible one", async () => {
    // Backgrounding is not staleness. Resolving the target after the await left
    // `loadingMoreHistory: true` pinned on the scrolling conversation — and since
    // a live entry is never re-bound on return, that guard then no-oped EVERY
    // future scroll-up: dead history until a page refresh.
    // One full window plus one page, so there is genuinely more to scroll to.
    const items: ConversationItem[] = Array.from(
      { length: INITIAL_WINDOW_ITEMS + SESSION_HISTORY_PAGE_SIZE },
      (_, idx) => userMessage(`bh_${idx.toString().padStart(4, "0")}`, `bh ${idx}`),
    );
    seedSession("conv_scroll", items);
    seedSession("conv_visible", []);
    await useChatStore.getState().switchTo("conv_scroll");
    const windowIds = useChatStore.getState().blocks.map((b) => b.ctx.itemId);

    let releasePage: (() => void) | null = null;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (/\/v1\/sessions\/conv_scroll\/items\?.*after=/.test(url) && releasePage === null) {
        return new Promise<Response>((resolve) => {
          releasePage = () => resolve(defaultFetchHandler(input, init));
        });
      }
      return defaultFetchHandler(input, init);
    });

    const page = useChatStore.getState().loadMoreHistory();
    expect(conversationRegistry.peek("conv_scroll")!.getState().loadingMoreHistory).toBe(true);

    // Switch away while the page is still open, then let it land.
    await useChatStore.getState().switchTo("conv_visible");
    releasePage!();
    await page;

    const scrolled = conversationRegistry.peek("conv_scroll")!.getState();
    // The flag cleared, so scroll-up still works when the user returns...
    expect(scrolled.loadingMoreHistory).toBe(false);
    // ...and the page landed on the conversation it belongs to, in order.
    expect(scrolled.blocks.map((b) => b.ctx.itemId)).toEqual([
      ...items.slice(0, SESSION_HISTORY_PAGE_SIZE).map((item) => item.id),
      ...windowIds,
    ]);
    // The visible conversation gained nothing.
    expect(useChatStore.getState().blocks).toEqual([]);
  });

  it("does not run flat session items through the nested snapshot flattener", async () => {
    seedSessionSnapshot("conv_native", []);
    seedSessionItems("conv_native", [nativeToolItem("resp_native")]);

    await useChatStore.getState().switchTo("conv_native");

    const native = useChatStore
      .getState()
      .blocks.find((block): block is NativeToolBlock => block.type === "native_tool");
    if (native === undefined) {
      throw new Error("expected native tool block");
    }
    expect(native.toolType).toBe("mcp_call");
    expect(native.data).toMatchObject({
      type: "mcp_call",
      name: "read_database",
      data: { rows: 3 },
    });
  });

  it("opens the session SSE stream during bind", async () => {
    seedSession("conv_abc");

    await useChatStore.getState().switchTo("conv_abc");

    const streamCall = fetchMock.mock.calls.find(([u]) =>
      String(u).endsWith("/v1/sessions/conv_abc/stream"),
    );
    expect(streamCall).toBeDefined();
  });

  it("switching to the same conversation is a no-op (blocks preserved, no stream re-open)", async () => {
    const block: AnyBlock = {
      type: "user_message",
      ctx: {
        agent: null,
        depth: 0,
        turn: 0,
        timestamp: 0,
        responseId: "resp_1",
        itemId: "msg_keep",
      },
      content: [{ type: "input_text", text: "preserved" }],
    };
    useChatStore.setState({ conversationId: "conv_abc", blocks: [block] });

    await useChatStore.getState().switchTo("conv_abc");

    expect(useChatStore.getState().blocks).toEqual([block]);
    expect(useChatStore.getState().conversationId).toBe("conv_abc");
    // Same-id guard must short-circuit BEFORE the stream open, otherwise
    // every URL-effect-driven re-call would churn a fresh connection.
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("aborts a conversation's stream when it is released, not when switched away", async () => {
    // Switch-away used to abort (see "does not abort a conversation's stream
    // when switching away"). Release — conversation deleted, or evicted from the
    // registry — is what tears the stream down now.
    const controller = new AbortController();
    useChatStore.setState({
      conversationId: "conv_abc",
      abortController: controller,
    });
    seedSession("conv_def");

    await useChatStore.getState().switchTo("conv_def");
    expect(controller.signal.aborted).toBe(false);
    expect(useChatStore.getState().conversationId).toBe("conv_def");

    releaseConversation("conv_abc");
    expect(controller.signal.aborted).toBe(true);
  });

  it("shows the hydrating placeholder while a COLD conversation binds", async () => {
    // `loadingConversation` unmounts the chat surface during hydration. It has
    // to be set for a cold entry: the composer would otherwise mount against
    // empty session-scoped state (`llmModel` / `sessionModelOverride` null) and
    // resolve its model label from the sticky cross-session pick — painting the
    // PREVIOUS conversation's model until the snapshot lands.
    const seen: boolean[] = [];
    const unsubscribe = useChatStore.subscribe((s) => seen.push(s.loadingConversation));
    // Hold every request so the hydrating window stays open to observe.
    fetchMock.mockImplementation(() => new Promise<Response>(() => {}));
    void useChatStore.getState().switchTo("conv_cold_hydrate");
    await tick();
    unsubscribe();

    expect(seen).toContain(true);
    expect(useChatStore.getState().loadingConversation).toBe(true);
  });

  it("does NOT show the placeholder when returning to a live conversation", async () => {
    // The converse, and the whole point of keeping streams open: a revisit
    // repaints the entry's own settled state, so flashing a spinner over an
    // already-current transcript would undo the snappiness this buys.
    seedSession("conv_warm", [userMessage("resp_w", "already here")]);
    await useChatStore.getState().switchTo("conv_warm");
    seedSession("conv_elsewhere", []);
    await useChatStore.getState().switchTo("conv_elsewhere");

    const seen: boolean[] = [];
    const unsubscribe = useChatStore.subscribe((s) => seen.push(s.loadingConversation));
    await useChatStore.getState().switchTo("conv_warm");
    unsubscribe();

    expect(seen).not.toContain(true);
    expect(useChatStore.getState().loadingConversation).toBe(false);
    // And it painted from the live entry, not from a refetch.
    expect(useChatStore.getState().blocks).toHaveLength(1);
  });

  it("switching to null clears state without opening a stream", async () => {
    const block: AnyBlock = {
      type: "user_message",
      ctx: {
        agent: null,
        depth: 0,
        turn: 0,
        timestamp: 0,
        responseId: "resp_1",
        itemId: "msg_x",
      },
      content: [{ type: "input_text", text: "x" }],
    };
    useChatStore.setState({
      conversationId: "conv_abc",
      blocks: [block],
      pendingUserMessages: [
        { tempId: "pend_unacked", content: [{ type: "input_text", text: "still waiting" }] },
      ],
    });

    await useChatStore.getState().switchTo(null);

    const state = useChatStore.getState();
    expect(state.conversationId).toBeNull();
    expect(state.blocks).toEqual([]);
    // Pending sends from the prior conversation must not leak into a
    // fresh chat surface — otherwise the user opens / and sees a
    // ghost bubble from the conv they just left.
    expect(state.pendingUserMessages).toEqual([]);
    expect(state.loadingConversation).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("ignores stale snapshot if a second switchTo races ahead", async () => {
    seedSession("conv_def", [
      userMessage("resp_def", "def turn"),
      assistantMessage("resp_def", "def reply"),
    ]);
    seedSession("conv_other");

    const first = useChatStore.getState().switchTo("conv_def");
    const second = useChatStore.getState().switchTo("conv_other");
    await Promise.all([first, second]);

    expect(useChatStore.getState().conversationId).toBe("conv_other");
    expect(useChatStore.getState().blocks).toEqual([]);
  });
});

describe("chatStore — send (first-send ordering)", () => {
  it("creates session → binds stream → posts message (in order)", async () => {
    seedSession("conv_new");
    const invalidateSpy = vi.spyOn(client, "invalidateQueries");

    let navigatedTo: string | null = null;
    await useChatStore.getState().send("hi", "agent_xyz", undefined, {
      onConversationCreated: (id) => {
        navigatedTo = id;
      },
    });

    // Order of fetch calls during first send: POST /v1/sessions →
    // GET /v1/runners → PATCH /v1/sessions/{id} →
    // GET /v1/sessions/{id}/stream → metadata/items hydration →
    // POST /v1/sessions/{id}/events.
    const calls = fetchMock.mock.calls.map(([u, init]) => ({
      url: String(u),
      method: (init as RequestInit | undefined)?.method ?? "GET",
    }));
    expect(calls[0]).toEqual({ url: "/v1/sessions", method: "POST" });
    expect(calls[1]).toEqual({ url: "/v1/runners", method: "GET" });
    expect(calls[2]).toEqual({ url: "/v1/sessions/conv_new", method: "PATCH" });
    expect(calls[3]?.url).toBe("/v1/sessions/conv_new/stream");
    const metadataIndex = calls.findIndex(
      (call) => call.url.split("?")[0] === "/v1/sessions/conv_new" && call.method === "GET",
    );
    const itemsIndex = calls.findIndex((call) =>
      call.url.startsWith("/v1/sessions/conv_new/items"),
    );
    const eventIndex = calls.findIndex(
      (call) => call.url === "/v1/sessions/conv_new/events" && call.method === "POST",
    );
    expect(metadataIndex).toBeGreaterThan(3);
    expect(itemsIndex).toBeGreaterThan(3);
    expect(eventIndex).toBeGreaterThan(Math.max(metadataIndex, itemsIndex));

    // POST /v1/sessions body: durable agent_id (NOT name) + empty initial_items.
    const createBody = JSON.parse((fetchMock.mock.calls[0]![1] as RequestInit).body as string);
    expect(createBody).toEqual({ agent_id: "agent_xyz", initial_items: [] });

    // POST /v1/events body: a user message event.
    const bindBody = JSON.parse((fetchMock.mock.calls[2]![1] as RequestInit).body as string);
    expect(bindBody).toEqual({ runner_id: "runner_ui_test" });

    const eventBody = JSON.parse(
      (fetchMock.mock.calls[eventIndex]![1] as RequestInit).body as string,
    );
    expect(eventBody).toEqual({
      type: "message",
      data: {
        role: "user",
        content: [{ type: "input_text", text: "hi" }],
      },
    });

    expect(navigatedTo).toBe("conv_new");
    expect(useChatStore.getState().conversationId).toBe("conv_new");

    // The user's input lands in `pendingUserMessages` (sidecar) with
    // a client-side tempId. It migrates into `blocks` via FIFO when
    // `session.input.consumed` arrives on the stream — that step is
    // covered separately in the handleSessionEvent tests.
    const state = useChatStore.getState();
    expect(state.pendingUserMessages).toHaveLength(1);
    expect(state.pendingUserMessages[0]!.content).toEqual([{ type: "input_text", text: "hi" }]);
    expect(state.pendingUserMessages[0]!.tempId).toMatch(/^pend_/);
    // No premature insertion into `blocks` — those are reserved for
    // server-acked + streamed content, plus snapshot hydration.
    expect(state.blocks.filter((b) => b.type === "user_message")).toHaveLength(0);

    expect(invalidateSpy).toHaveBeenCalledTimes(2);
    expect(invalidateSpy.mock.calls.map(([arg]) => arg)).toEqual([
      { queryKey: ["conversations"] },
      { queryKey: ["conversations"] },
    ]);
  });

  it("serializes rapid-fire sends so POSTs reach the server in submission order", async () => {
    // Existing, already-bound conversation: send() skips session-create and
    // the stream rebind, so the only network call is POST /events.
    useChatStore.setState({
      conversationId: "conv_x",
      abortController: new AbortController(),
      status: "idle",
    });

    // Control /events resolution: each POST returns a deferred promise so the
    // test can hold the first one open and observe whether the next fires.
    const eventBodies: string[] = [];
    const resolvers: (() => void)[] = [];
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/v1/sessions/conv_x/events" && init?.method === "POST") {
        eventBodies.push(init.body as string);
        return new Promise<Response>((resolve) => {
          resolvers.push(() => resolve(mockResponse({ queued: true, item_id: "ci_mock" })));
        });
      }
      return defaultFetchHandler(input, init);
    });

    const textOf = (body: string): string =>
      (JSON.parse(body).data.content[0] as { text: string }).text;

    // Fire three sends WITHOUT awaiting — simulates rapid Enter presses.
    const p1 = useChatStore.getState().send("1", "agent_xyz");
    const p2 = useChatStore.getState().send("2", "agent_xyz");
    const p3 = useChatStore.getState().send("3", "agent_xyz");

    await tick();
    // Serialization invariant: only the first POST is in flight; the second
    // and third are blocked on the prior send's completion. Without
    // serialization all three concurrent POSTs would already have fired,
    // and the server could accept them out of order.
    expect(eventBodies.map(textOf)).toEqual(["1"]);

    resolvers[0]!(); // complete send 1's POST → unblocks send 2
    await tick();
    expect(eventBodies.map(textOf)).toEqual(["1", "2"]);

    resolvers[1]!(); // complete send 2's POST → unblocks send 3
    await tick();
    expect(eventBodies.map(textOf)).toEqual(["1", "2", "3"]);

    resolvers[2]!();
    await Promise.all([p1, p2, p3]);

    // Final delivery order is exactly submission order.
    expect(eventBodies.map(textOf)).toEqual(["1", "2", "3"]);
  });

  it("serializes a second send issued while a NEW chat's session is still being created", async () => {
    // The narrowest ordering window, and the one a per-conversation chain
    // opens: the first send from `/` has no conversation id, so it takes a slot
    // under the new-session key. `ensureBoundSession` then publishes the real id
    // to the store BEFORE that send's POST — so a second send fired in that
    // window pins the real id and would key on an EMPTY chain, overtaking the
    // first. The first send's slot has to reach the real id before the id
    // becomes visible to anyone else.
    const eventBodies: string[] = [];
    const resolvers: (() => void)[] = [];
    seedSession("conv_new");
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/v1/sessions/conv_new/events" && init?.method === "POST") {
        eventBodies.push(init.body as string);
        return new Promise<Response>((resolve) => {
          resolvers.push(() => resolve(mockResponse({ queued: true, item_id: "ci_mock" })));
        });
      }
      return defaultFetchHandler(input, init);
    });
    const textOf = (body: string): string =>
      (JSON.parse(body).data.content[0] as { text: string }).text;

    // Hold the bind's snapshot GET so `ensureBoundSession` parks INSIDE
    // `bindStream` — after it published the real id to the store, but before
    // the first send reaches its POST. That is the window, and it lasts as long
    // as the bind's network work does.
    let releaseSnapshot: (() => void) | null = null;
    const baseHandler = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      // Only the FIRST snapshot GET is held; later ones (the second send's
      // rebind check) pass through, so the latch isolates the window under test.
      if (
        /^\/v1\/sessions\/conv_new(\?|$)/.test(url) &&
        (init?.method ?? "GET") === "GET" &&
        releaseSnapshot === null
      ) {
        return new Promise<Response>((resolve) => {
          releaseSnapshot = () => resolve(baseHandler(input, init) as Promise<Response>);
        });
      }
      return baseHandler(input, init);
    });

    // First send from the landing route (conversationId === null).
    const p1 = useChatStore.getState().send("first", "agent_xyz");
    await tick();
    await tick();
    await tick();
    // The id is visible to any other send, but the first send is still parked
    // in its bind and has not reached `rekey` yet.
    expect(useChatStore.getState().conversationId).toBe("conv_new");
    expect(releaseSnapshot).not.toBeNull();

    // Second send now resolves `conv_new` as its own submit-time target.
    const p2 = useChatStore.getState().send("second", "agent_xyz");
    await tick();
    await tick();

    // The first send is still parked in its bind, so ITS post hasn't fired.
    // The second must not overtake it: pre-fix it keyed an empty chain (the
    // first send's slot was still under the new-session key) and fired here,
    // reaching the server before "first".
    expect(eventBodies.map(textOf)).toEqual([]);

    releaseSnapshot!();
    await tick();
    await tick();
    // The bind finished; the first send's POST goes out first, alone.
    expect(eventBodies.map(textOf)).toEqual(["first"]);

    // Releasing the first POST hands the chain to the second, which then runs
    // its own bind check + upload before posting.
    resolvers[0]!();
    /* oxlint-disable no-await-in-loop */
    for (let i = 0; i < 6; i++) await tick();
    /* oxlint-enable no-await-in-loop */
    expect(eventBodies.map(textOf)).toEqual(["first", "second"]);

    resolvers[1]!();
    await Promise.all([p1, p2]);
    // Delivery order is submission order.
    expect(eventBodies.map(textOf)).toEqual(["first", "second"]);
  });

  it("keeps three sends ordered across a new chat's id publication", async () => {
    // The chain has to migrate as ONE unit, carrying its already-queued
    // followers. Send 2 queues under the new-session key (before the id exists);
    // send 3 arrives after the id is published, so it keys the real id directly.
    // Moving only the creating send's slot left send 3 waiting on send 1 rather
    // than on send 2 — so both became runnable at once.
    //
    // The invariant is strict serialization, asserted by leaving send 2's POST
    // UNRESOLVED: send 3 must not reach the wire until send 2's has returned.
    // (Asserting only the final array can't catch this — with both in flight
    // the mock's microtask order still happens to append them in sequence,
    // while against a real server the two are racing.)
    const eventBodies: string[] = [];
    const resolvers: (() => void)[] = [];
    seedSession("conv_new");
    const baseHandler = fetchMock.getMockImplementation()!;
    let releaseSnapshot: (() => void) | null = null;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/v1/sessions/conv_new/events" && init?.method === "POST") {
        eventBodies.push(init.body as string);
        return new Promise<Response>((resolve) => {
          resolvers.push(() => resolve(mockResponse({ queued: true, item_id: "ci_mock" })));
        });
      }
      // Hold the first bind's snapshot so send 1 parks inside `bindStream` with
      // the id already published — the window both followers land in.
      if (
        /^\/v1\/sessions\/conv_new(\?|$)/.test(url) &&
        (init?.method ?? "GET") === "GET" &&
        releaseSnapshot === null
      ) {
        return new Promise<Response>((resolve) => {
          releaseSnapshot = () => resolve(baseHandler(input, init) as Promise<Response>);
        });
      }
      return baseHandler(input, init);
    });
    const textOf = (body: string): string =>
      (JSON.parse(body).data.content[0] as { text: string }).text;

    // Sends 1 and 2 are submitted in the SAME tick, from the landing route: no
    // id exists yet, so both take slots under the new-session key. This has to
    // be synchronous — `rekey` fires as soon as `createSession` returns, so any
    // await here would let send 2 key the real id and miss the case under test.
    const p1 = useChatStore.getState().send("1", "agent_xyz");
    const p2 = useChatStore.getState().send("2", "agent_xyz");
    /* oxlint-disable no-await-in-loop */
    for (let i = 0; i < 3; i++) await tick();
    /* oxlint-enable no-await-in-loop */
    // The id is now visible, and send 1 is parked in its bind.
    expect(useChatStore.getState().conversationId).toBe("conv_new");
    expect(releaseSnapshot).not.toBeNull();
    // Send 3 resolves the real id and must queue behind send 2, not send 1.
    const p3 = useChatStore.getState().send("3", "agent_xyz");
    await tick();
    await tick();
    expect(eventBodies.map(textOf)).toEqual([]);

    releaseSnapshot!();
    /* oxlint-disable no-await-in-loop */
    for (let i = 0; i < 4; i++) await tick();
    /* oxlint-enable no-await-in-loop */
    // Send 1 posts alone — the followers are still chained behind it.
    expect(eventBodies.map(textOf)).toEqual(["1"]);

    // Release ONLY send 1. Send 2 may now post; send 3 must stay parked, because
    // its predecessor's POST has not returned. Pre-fix, send 3 waited on send 1
    // too, so it posts here alongside send 2 — two writes racing on one session.
    resolvers[0]!();
    /* oxlint-disable no-await-in-loop */
    for (let i = 0; i < 10; i++) await tick();
    /* oxlint-enable no-await-in-loop */
    expect(eventBodies.map(textOf)).toEqual(["1", "2"]);

    // Only now that send 2's POST has returned may send 3 go out.
    resolvers[1]!();
    /* oxlint-disable no-await-in-loop */
    for (let i = 0; i < 10; i++) await tick();
    /* oxlint-enable no-await-in-loop */
    expect(eventBodies.map(textOf)).toEqual(["1", "2", "3"]);

    resolvers[2]!();
    await Promise.all([p1, p2, p3]);
    expect(eventBodies.map(textOf)).toEqual(["1", "2", "3"]);
  });

  it("PATCHes sticky effort onto a brand-new session before binding the runner", async () => {
    seedSession("conv_new");
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/v1/sessions" && init?.method === "POST") {
        const body = init?.body;
        const parsed = body
          ? (JSON.parse(body as string) as { agent_id: string })
          : { agent_id: "?" };
        return mockResponse({
          id: "conv_new",
          agent_id: parsed.agent_id,
          status: "idle",
          created_at: 0,
          items: [],
          labels: { "omnigent.wrapper": "claude-code-native-ui" },
        });
      }
      return defaultFetchHandler(input, init);
    });
    useChatStore.setState({ selectedEffort: "max" });

    await useChatStore.getState().send("hi", "agent_xyz");

    // Effort must be persisted before runner bind.
    const calls = fetchMock.mock.calls.map(([u, init]) => ({
      url: String(u),
      method: (init as RequestInit | undefined)?.method ?? "GET",
      body: (init as RequestInit | undefined)?.body
        ? JSON.parse((init as RequestInit).body as string)
        : undefined,
    }));
    // 0: POST /v1/sessions (create)
    // 1: PATCH /v1/sessions/{id} (effort, silent)
    // 2: GET /v1/runners (find runner)
    // 3: PATCH /v1/sessions/{id} (bind runner)
    expect(calls[0]).toMatchObject({ url: "/v1/sessions", method: "POST" });
    expect(calls[1]).toMatchObject({
      url: "/v1/sessions/conv_new",
      method: "PATCH",
      body: { reasoning_effort: "max", silent: true },
    });
    expect(calls[2]).toMatchObject({ url: "/v1/runners", method: "GET" });
  });

  it("does not PATCH sticky effort onto a brand-new custom session", async () => {
    seedSession("conv_new");
    useChatStore.setState({ selectedEffort: "max" });

    await useChatStore.getState().send("hi", "agent_xyz");

    const calls = fetchMock.mock.calls.map(([u, init]) => ({
      url: String(u),
      method: (init as RequestInit | undefined)?.method ?? "GET",
      body: (init as RequestInit | undefined)?.body
        ? JSON.parse((init as RequestInit).body as string)
        : undefined,
    }));
    expect(calls[0]).toMatchObject({ url: "/v1/sessions", method: "POST" });
    // If sticky effort leaked onto custom sessions, that PATCH would occupy index 1.
    expect(calls[1]).toMatchObject({ url: "/v1/runners", method: "GET" });
    expect(
      calls.some(
        (call) =>
          call.url === "/v1/sessions/conv_new" &&
          call.method === "PATCH" &&
          "reasoning_effort" in (call.body ?? {}),
      ),
    ).toBe(false);
  });

  it("existing-session send only posts an event (no createSession, no stream open)", async () => {
    // Simulate a session already bound: state has conversationId +
    // abortController set. send must NOT createSession or openStream.
    useChatStore.setState({
      conversationId: "conv_existing",
      abortController: new AbortController(),
    });

    await useChatStore.getState().send("follow up", "agent_xyz");

    const calls = fetchMock.mock.calls.map(([u, init]) => ({
      url: String(u),
      method: (init as RequestInit | undefined)?.method ?? "GET",
    }));
    expect(calls).toEqual([{ url: "/v1/sessions/conv_existing/events", method: "POST" }]);
  });

  it("invalidates conversations after every postEvent (existing-session sidebar reorder)", async () => {
    // Without this, the sender's chat lags 0-4 s behind their own message.
    useChatStore.setState({
      conversationId: "conv_existing",
      abortController: new AbortController(),
    });
    const invalidateSpy = vi.spyOn(client, "invalidateQueries");

    await useChatStore.getState().send("follow up", "agent_xyz");

    const conversationInvalidations = invalidateSpy.mock.calls.filter(
      ([arg]) =>
        typeof arg === "object" &&
        arg !== null &&
        "queryKey" in arg &&
        Array.isArray((arg as { queryKey: unknown[] }).queryKey) &&
        (arg as { queryKey: unknown[] }).queryKey[0] === "conversations",
    );
    expect(conversationInvalidations).toHaveLength(1);
  });

  it("rebinds the SSE stream before posting when the controller was cleared (idle disconnect)", async () => {
    // Post-disconnect state: session is still selected, but the pump
    // already exited (e.g. idle proxy closed the connection) and
    // cleared abortController. `send` must rebind before POSTing or
    // the response events publish into an empty subscriber set.
    useChatStore.setState({
      conversationId: "conv_existing",
      abortController: null,
    });

    await useChatStore.getState().send("after disconnect", "agent_xyz");

    const calls = fetchMock.mock.calls.map(([u, init]) => ({
      url: String(u),
      method: (init as RequestInit | undefined)?.method ?? "GET",
    }));
    // Order: open stream → metadata/items hydration → POST event.
    // Asserting position (not just presence) is the proof that the
    // rebind happened BEFORE the POST.
    expect(calls[0]?.url).toBe("/v1/sessions/conv_existing/stream");
    const metadataIndex = calls.findIndex(
      (call) => call.url.split("?")[0] === "/v1/sessions/conv_existing" && call.method === "GET",
    );
    const itemsIndex = calls.findIndex((call) =>
      call.url.startsWith("/v1/sessions/conv_existing/items"),
    );
    const eventIndex = calls.findIndex(
      (call) => call.url === "/v1/sessions/conv_existing/events" && call.method === "POST",
    );
    expect(metadataIndex).toBeGreaterThan(0);
    expect(itemsIndex).toBeGreaterThan(0);
    expect(eventIndex).toBeGreaterThan(Math.max(metadataIndex, itemsIndex));

    // The optimistic bubble survives — the POST succeeded.
    expect(useChatStore.getState().pendingUserMessages).toHaveLength(1);
  });

  it("tears the old pump down before rebinding after a failed snapshot", async () => {
    // A snapshot failure leaves `conversationLoadError` set while the pump it
    // opened is STILL RUNNING — `bindStream` catches the error without aborting
    // its controller. Rebinding blind then replaced the stored controller and
    // left two subscribers on one entry, so any delta with no item id to dedupe
    // on (live claude-native text) applied twice.
    seedSession("conv_two_subs", []);
    let failSnapshot = true;
    const openedStreams: AbortSignal[] = [];
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/v1/sessions/conv_two_subs/stream") {
        if (init?.signal) openedStreams.push(init.signal);
        return mockResponse(null, { bodyStream: pushableStream().stream });
      }
      // Fail only the FIRST snapshot GET, so the bind errors with its pump live.
      if (url.split("?")[0] === "/v1/sessions/conv_two_subs" && (init?.method ?? "GET") === "GET") {
        if (failSnapshot) {
          failSnapshot = false;
          return mockResponse({}, { ok: false, status: 500 });
        }
      }
      return defaultFetchHandler(input, init);
    });

    await useChatStore.getState().switchTo("conv_two_subs");
    const entry = conversationRegistry.peek("conv_two_subs")!;
    // The failed bind: error recorded, but its pump is still open.
    expect(entry.getState().conversationLoadError).not.toBeNull();
    expect(openedStreams).toHaveLength(1);
    expect(openedStreams[0]!.aborted).toBe(false);

    // Sending rebinds, because the entry is not stream-current.
    await useChatStore.getState().send("after a failed load", "agent_xyz");

    // The first pump was aborted rather than orphaned — one live subscriber.
    expect(openedStreams).toHaveLength(2);
    expect(openedStreams[0]!.aborted).toBe(true);
    expect(openedStreams[1]!.aborted).toBe(false);
    expect(entry.getState().conversationLoadError).toBeNull();
  });

  it("degrades gracefully when stream rebind fails — message still posts", async () => {
    useChatStore.setState({
      conversationId: "conv_existing",
      abortController: null,
    });
    // Make the stream-open call fail. With graceful degradation,
    // bindStream logs the failure but doesn't block — send proceeds
    // to post the message. The user sees history in read-only mode
    // and the message reaches the server even without a live stream.
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/v1/sessions/conv_existing/stream")) {
        return mockResponse({}, { ok: false, status: 500 });
      }
      return defaultFetchHandler(input, init);
    });

    await useChatStore.getState().send("still lands", "agent_xyz");

    // The message was posted — an /events POST should have fired.
    const eventPosts = fetchMock.mock.calls.filter(([u, init]) => {
      return String(u).endsWith("/events") && (init as RequestInit | undefined)?.method === "POST";
    });
    expect(eventPosts).toHaveLength(1);
  });

  it("pushes the optimistic message bubble before any await", async () => {
    useChatStore.setState({
      conversationId: "conv_existing",
      abortController: new AbortController(),
      pendingUserMessages: [],
    });

    const sendPromise = useChatStore.getState().send("hi", "agent_xyz");
    // The optimistic bubble is pushed synchronously, before any network
    // call resolves, so the user sees their message immediately.
    expect(useChatStore.getState().pendingUserMessages).toHaveLength(1);
    await sendPromise;
  });

  it("rolls back the optimistic message bubble when postEvent throws", async () => {
    useChatStore.setState({
      conversationId: "conv_existing",
      abortController: new AbortController(),
      pendingUserMessages: [],
    });
    fetchMock.mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith("/v1/sessions/conv_existing/events")) {
        return mockResponse({ error: "boom" }, { ok: false, status: 500 });
      }
      return defaultFetchHandler(input, init);
    });

    await useChatStore.getState().send("hi", "agent_xyz");
    // The bubble pushed before the POST is removed on failure — no server
    // idle will fire to reconcile it.
    expect(useChatStore.getState().pendingUserMessages).toEqual([]);
  });

  it("surfaces a visible error block with friendly copy when the runner is unavailable (503)", async () => {
    // The fresh-send failure mode: POST /events 503s because a host-bound
    // runner never came online. Before this, finalizeActive was a no-op (no
    // activeResponse) so the user was left on a silent, empty composer. Now
    // the failure must render as an error block explaining what happened.
    useChatStore.setState({
      conversationId: "conv_existing",
      abortController: new AbortController(),
      status: "idle",
      sessionStatus: "running",
      blocks: [],
      pendingUserMessages: [],
    });
    fetchMock.mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith("/v1/sessions/conv_existing/events")) {
        // Exact AP error wire shape: {"error": {"code", "message"}} @ 503.
        return mockResponse(
          { error: { code: "runner_unavailable", message: "No runner bound for session" } },
          { ok: false, status: 503 },
        );
      }
      return defaultFetchHandler(input, init);
    });

    await useChatStore.getState().send("hi", "agent_xyz");

    const state = useChatStore.getState();
    // Optimistic bubble rolled back, turn settled to idle.
    expect(state.pendingUserMessages).toEqual([]);
    expect(state.status).toBe("idle");
    expect(state.sessionStatus).toBe("idle");
    // A standalone error block is appended carrying the friendly, retryable
    // copy — NOT the server's terse "No runner bound for session" — and no
    // raw code in the banner (code "" → clean "Error" title).
    const errorBlocks = state.blocks.filter((b) => b.type === "error");
    expect(errorBlocks).toHaveLength(1);
    expect(errorBlocks[0]).toMatchObject({
      type: "error",
      message: "The runner didn't come online in time. Please try again.",
      code: "",
    });
  });

  it("carries a non-runner send failure's own message into the error block", async () => {
    // A generic failure (not runner_unavailable) must still become visible,
    // using the server-provided message and code so it isn't swallowed.
    useChatStore.setState({
      conversationId: "conv_existing",
      abortController: new AbortController(),
      status: "idle",
      blocks: [],
      pendingUserMessages: [],
    });
    fetchMock.mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith("/v1/sessions/conv_existing/events")) {
        return mockResponse(
          { error: { code: "internal_error", message: "boom on the server" } },
          { ok: false, status: 500 },
        );
      }
      return defaultFetchHandler(input, init);
    });

    await useChatStore.getState().send("hi", "agent_xyz");

    const errorBlocks = useChatStore.getState().blocks.filter((b) => b.type === "error");
    expect(errorBlocks).toHaveLength(1);
    expect(errorBlocks[0]).toMatchObject({
      type: "error",
      message: "boom on the server",
      code: "internal_error",
    });
  });

  it("settles optimistic pending state when an input policy denies the send", async () => {
    useChatStore.setState({
      conversationId: "conv_existing",
      abortController: new AbortController(),
      status: "idle",
      sessionStatus: "running",
    });
    fetchMock.mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith("/v1/sessions/conv_existing/events")) {
        return mockResponse({ queued: false, denied: true });
      }
      return defaultFetchHandler(input, init);
    });

    await useChatStore.getState().send("blocked by policy", "agent_xyz");

    const state = useChatStore.getState();
    // A denied INPUT policy does not persist the user message, so no
    // session.input.consumed can promote or remove the optimistic bubble.
    // The POST response itself must settle the pending entry.
    expect(state.pendingUserMessages).toEqual([]);
    // The denied POST is terminal for this attempted turn; leaving the
    // local streaming flag or sessionStatus live would keep the active
    // sidebar row running without a server turn to finish it.
    expect(state.status).toBe("idle");
    expect(state.sessionStatus).toBe("idle");
  });

  it("does not settle a later active chat when an old denied send resolves after navigation", async () => {
    useChatStore.setState({
      conversationId: "conv_existing",
      abortController: new AbortController(),
      status: "idle",
      sessionStatus: "idle",
    });
    let resolvePost: (() => void) | null = null;
    fetchMock.mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith("/v1/sessions/conv_existing/events")) {
        return new Promise<Response>((resolve) => {
          resolvePost = () => resolve(mockResponse({ queued: false, denied: true }));
        });
      }
      return defaultFetchHandler(input, init);
    });

    const sendPromise = useChatStore.getState().send("blocked by policy", "agent_xyz");
    await tick();
    // The POST for conv_existing is paused; if it resolves after
    // navigation, its cleanup must not mutate the newly active chat.
    expect(resolvePost).not.toBeNull();
    seedSession("conv_other");
    await useChatStore.getState().switchTo("conv_other");
    useChatStore.setState({
      pendingUserMessages: [
        { tempId: "pend_other", content: [{ type: "input_text", text: "other pending" }] },
      ],
      status: "streaming",
      sessionStatus: "running",
    });

    resolvePost!();
    await sendPromise;

    const state = useChatStore.getState();
    // These values belong to conv_other. A stale denied POST from
    // conv_existing must leave them byte-for-byte intact.
    expect(state.conversationId).toBe("conv_other");
    expect(state.pendingUserMessages).toEqual([
      { tempId: "pend_other", content: [{ type: "input_text", text: "other pending" }] },
    ]);
    expect(state.status).toBe("streaming");
    expect(state.sessionStatus).toBe("running");
  });

  it("duplicate-bind: navigation-triggered switchTo after first send is a no-op", async () => {
    // After send creates a session, it sets conversationId and calls
    // bindStream. The caller's onConversationCreated navigates to
    // /c/:newId, which makes ChatPage's URL effect call switchTo(newId).
    // That call must hit the same-id guard, NOT open a second stream.
    seedSession("conv_new");

    await useChatStore.getState().send("hi", "agent_xyz", undefined, {
      onConversationCreated: () => {
        // Synchronous: model the URL-effect calling switchTo here. It
        // should return immediately via the conversationId === id guard
        // (which `send` set before invoking this callback).
        void useChatStore.getState().switchTo("conv_new");
      },
    });

    const streamOpens = fetchMock.mock.calls.filter(([u]) =>
      String(u).endsWith("/v1/sessions/conv_new/stream"),
    );
    expect(streamOpens).toHaveLength(1);
  });
});

describe("chatStore — sendSlashCommand", () => {
  /** Parse the JSON body of the single POST /events call. */
  function lastEventBody(): { type: string; data: Record<string, unknown> } {
    const post = fetchMock.mock.calls.find(
      ([u, init]) =>
        String(u).endsWith("/events") && (init as RequestInit | undefined)?.method === "POST",
    );
    if (!post) throw new Error("no POST /events call recorded");
    return JSON.parse((post[1] as RequestInit).body as string);
  }

  it("posts a slash_command event with the skill name and arguments", async () => {
    useChatStore.setState({
      conversationId: "conv_existing",
      abortController: new AbortController(),
    });

    await useChatStore.getState().sendSlashCommand("grill-me", "review this plan", "agent_xyz");

    // The wire shape must match the REPL's: type=slash_command, kind=skill,
    // name without leading slash, and the raw argument text. A regression to
    // a plaintext message would set type="message" and fail here.
    expect(lastEventBody()).toEqual({
      type: "slash_command",
      data: { kind: "skill", name: "grill-me", arguments: "review this plan" },
    });
  });

  it("sends empty arguments when the skill is invoked with no args", async () => {
    useChatStore.setState({
      conversationId: "conv_existing",
      abortController: new AbortController(),
    });

    await useChatStore.getState().sendSlashCommand("deslop", "", "agent_xyz");

    expect(lastEventBody().data).toEqual({ kind: "skill", name: "deslop", arguments: "" });
  });

  it("sets streaming status and pushes an optimistic echo of the typed command", async () => {
    useChatStore.setState({
      conversationId: "conv_existing",
      abortController: new AbortController(),
      status: "idle",
    });

    // Hold the POST open so we can observe state between send and ack.
    let resolvePost: () => void = () => {};
    fetchMock.mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith("/v1/sessions/conv_existing/events")) {
        return new Promise<Response>((resolve) => {
          resolvePost = () => resolve(mockResponse({ queued: true, item_id: "ci_mock" }));
        });
      }
      return defaultFetchHandler(input, init);
    });

    const p = useChatStore.getState().sendSlashCommand("grill-me", "review this", "agent_xyz");
    await tick();

    // The typed "/name args" renders immediately as an optimistic bubble —
    // while this POST is held the server may still be booting a runner and
    // resolving the skill (the skill-first-session case), and without the
    // echo the chat sat on the empty state until the receipt arrived. The
    // pump's slash_command case pops this entry when the receipt lands.
    expect(useChatStore.getState().status).toBe("streaming");
    const pending = useChatStore.getState().pendingUserMessages;
    expect(pending).toHaveLength(1);
    expect(pending[0]!.content).toEqual([{ type: "input_text", text: "/grill-me review this" }]);

    resolvePost();
    await p;
  });

  it("settles local working state when an input policy denies a slash command", async () => {
    useChatStore.setState({
      conversationId: "conv_existing",
      abortController: new AbortController(),
      status: "idle",
      sessionStatus: "running",
    });
    fetchMock.mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith("/v1/sessions/conv_existing/events")) {
        return mockResponse({ queued: false, denied: true });
      }
      return defaultFetchHandler(input, init);
    });

    await useChatStore.getState().sendSlashCommand("grill-me", "", "agent_xyz");

    const state = useChatStore.getState();
    // A denied command publishes no receipt, so nothing would ever pop the
    // optimistic echo — the denial path must roll it back itself (a stuck
    // entry here means a phantom "/grill-me" bubble lingers after the
    // denial), and reset the local streaming flag + sessionStatus because
    // the server will not start a response turn.
    expect(state.pendingUserMessages).toEqual([]);
    expect(state.status).toBe("idle");
    expect(state.sessionStatus).toBe("idle");
  });

  it("does not settle a later active chat when an old denied slash command resolves", async () => {
    useChatStore.setState({
      conversationId: "conv_existing",
      abortController: new AbortController(),
      status: "idle",
      sessionStatus: "idle",
    });
    let resolvePost: (() => void) | null = null;
    fetchMock.mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith("/v1/sessions/conv_existing/events")) {
        return new Promise<Response>((resolve) => {
          resolvePost = () => resolve(mockResponse({ queued: false, denied: true }));
        });
      }
      return defaultFetchHandler(input, init);
    });

    const sendPromise = useChatStore.getState().sendSlashCommand("grill-me", "", "agent_xyz");
    await tick();
    // The old slash POST is still unresolved. Navigating away before it
    // denies should make the eventual cleanup a no-op for the new chat.
    expect(resolvePost).not.toBeNull();
    seedSession("conv_other");
    await useChatStore.getState().switchTo("conv_other");
    useChatStore.setState({
      status: "streaming",
      sessionStatus: "running",
    });

    resolvePost!();
    await sendPromise;

    const state = useChatStore.getState();
    // A stale slash denial from conv_existing must not idle the
    // currently active conv_other state.
    expect(state.conversationId).toBe("conv_other");
    expect(state.status).toBe("streaming");
    expect(state.sessionStatus).toBe("running");
  });

  it("rolls back a denied slash command on the conversation that sent it", async () => {
    // The converse of the test above: settling must be routed, not skipped. The
    // denial has to land on the sending conversation even once it is
    // backgrounded, or its echo hangs there forever with nothing to pop it (a
    // denied command publishes no receipt).
    seedSession("conv_slash_bg", []);
    seedSession("conv_slash_other", []);
    let resolvePost: (() => void) | null = null;
    fetchMock.mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith("/v1/sessions/conv_slash_bg/events")) {
        return new Promise<Response>((resolve) => {
          resolvePost = () => resolve(mockResponse({ queued: false, denied: true }));
        });
      }
      return defaultFetchHandler(input, init);
    });

    await useChatStore.getState().switchTo("conv_slash_bg");
    const sending = useChatStore.getState().sendSlashCommand("grill-me", "", "agent_xyz");
    await tick();
    expect(conversationRegistry.peek("conv_slash_bg")!.getState().pendingUserMessages).toHaveLength(
      1,
    );

    await useChatStore.getState().switchTo("conv_slash_other");
    resolvePost!();
    await sending;

    // The sender's echo rolled back and its status settled...
    const sender = conversationRegistry.peek("conv_slash_bg")!.getState();
    expect(sender.pendingUserMessages).toEqual([]);
    expect(sender.status).toBe("idle");
    expect(sender.sessionStatus).toBe("idle");
    // ...without disturbing the conversation now on screen.
    expect(useChatStore.getState().conversationId).toBe("conv_slash_other");
  });

  it("rolls back a failed slash command on the conversation that sent it", async () => {
    // Same for a thrown POST: the old code skipped the rollback entirely once
    // the sender was backgrounded, stranding the echo and leaving `status`
    // pinned at "streaming" for that conversation.
    seedSession("conv_slash_fail", []);
    seedSession("conv_slash_elsewhere", []);
    let rejectPost: (() => void) | null = null;
    fetchMock.mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith("/v1/sessions/conv_slash_fail/events")) {
        return new Promise<Response>((resolve) => {
          rejectPost = () => resolve(mockResponse({ error: "boom" }, { ok: false, status: 500 }));
        });
      }
      return defaultFetchHandler(input, init);
    });

    await useChatStore.getState().switchTo("conv_slash_fail");
    const sending = useChatStore.getState().sendSlashCommand("deslop", "", "agent_xyz");
    await tick();

    await useChatStore.getState().switchTo("conv_slash_elsewhere");
    rejectPost!();
    await sending;

    const sender = conversationRegistry.peek("conv_slash_fail")!.getState();
    expect(sender.pendingUserMessages).toEqual([]);
    expect(sender.status).toBe("idle");
  });

  it("creates and binds a brand-new session before posting the slash_command", async () => {
    // First-message-is-a-skill path: no conversation yet. Must create the
    // session + bind the stream (like send) before POSTing.
    seedSession("conv_new");

    let createdId: string | null = null;
    await useChatStore.getState().sendSlashCommand("grill-me", "", "agent_xyz", {
      onConversationCreated: (id) => {
        createdId = id;
      },
    });

    expect(createdId).toBe("conv_new");
    const calls = fetchMock.mock.calls.map(([u, init]) => ({
      url: String(u),
      method: (init as RequestInit | undefined)?.method ?? "GET",
    }));
    // Session created, runner bound, stream opened, then the event posted.
    expect(calls[0]).toMatchObject({ url: "/v1/sessions", method: "POST" });
    // The runner-discovery GET proves bindOnlyOnlineRunner ran — without it
    // the POST /events would queue into a session with no executor. (If this
    // step were dropped the create + callback would still fire, so assert it
    // explicitly rather than relying on createdId.)
    expect(calls.some((c) => c.url === "/v1/runners" && c.method === "GET")).toBe(true);
    expect(calls.some((c) => c.url === "/v1/sessions/conv_new/stream")).toBe(true);
    expect(calls.at(-1)).toMatchObject({
      url: "/v1/sessions/conv_new/events",
      method: "POST",
    });
  });

  it("resets the streaming status when the POST fails", async () => {
    useChatStore.setState({
      conversationId: "conv_existing",
      abortController: new AbortController(),
      status: "idle",
    });
    fetchMock.mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith("/v1/sessions/conv_existing/events")) {
        return mockResponse({ error: "boom" }, { ok: false, status: 500 });
      }
      return defaultFetchHandler(input, init);
    });

    await useChatStore.getState().sendSlashCommand("grill-me", "", "agent_xyz");
    // No server idle will fire for a failed POST, so the action resets its
    // own local streaming flag.
    expect(useChatStore.getState().status).toBe("idle");
    // And rolls back the optimistic echo — no receipt will ever pop it.
    expect(useChatStore.getState().pendingUserMessages).toEqual([]);
  });
});

describe("chatStore — send while streaming (queueing)", () => {
  it("posts the queued event without resetting activeResponse or status", async () => {
    // Simulate an in-flight response: status streaming, an active
    // bubble bound to resp_in_flight. Sending another message must
    // queue server-side and leave the in-flight bubble's lifecycle
    // untouched (any reset would flicker its rendered state from
    // "streaming" → null → "streaming" on the next response.created).
    useChatStore.setState({
      conversationId: "conv_abc",
      abortController: new AbortController(),
      status: "streaming",
      activeResponse: { responseId: "resp_in_flight", state: "streaming", error: null },
    });

    await useChatStore.getState().send("queue me", "agent_xyz");

    const events = fetchMock.mock.calls.filter(([u]) =>
      String(u).endsWith("/v1/sessions/conv_abc/events"),
    );
    expect(events).toHaveLength(1);
    const body = JSON.parse((events[0]![1] as RequestInit).body as string);
    expect(body).toEqual({
      type: "message",
      data: { role: "user", content: [{ type: "input_text", text: "queue me" }] },
    });

    const state = useChatStore.getState();
    expect(state.status).toBe("streaming");
    expect(state.activeResponse).toEqual({
      responseId: "resp_in_flight",
      state: "streaming",
      error: null,
    });

    // Queued message goes into the pending sidecar; nothing touches
    // `blocks` until session.input.consumed promotes it. Keeping it off
    // `blocks` is what lets the still-streaming response 1 keep appending
    // its events cleanly at the tail.
    expect(state.pendingUserMessages).toHaveLength(1);
    expect(state.pendingUserMessages[0]!.content).toEqual([
      { type: "input_text", text: "queue me" },
    ]);
    expect(state.blocks.filter((b) => b.type === "user_message")).toHaveLength(0);
  });

  it("rolls back the pending entry when the queue POST fails", async () => {
    useChatStore.setState({
      conversationId: "conv_abc",
      abortController: new AbortController(),
      status: "streaming",
      activeResponse: { responseId: "resp_in_flight", state: "streaming", error: null },
    });
    fetchMock.mockImplementationOnce(() => mockResponse({}, { ok: false, status: 500 }));

    await useChatStore.getState().send("flaky", "agent_xyz");

    const state = useChatStore.getState();
    // The pending entry inserted optimistically must be removed on
    // failure — otherwise a phantom bubble lingers for a message the
    // server never accepted.
    expect(state.pendingUserMessages).toEqual([]);
    // Active response state is unchanged — failures while queueing
    // must not poison the still-streaming prior response's lifecycle.
    expect(state.status).toBe("streaming");
    expect(state.activeResponse).toEqual({
      responseId: "resp_in_flight",
      state: "streaming",
      error: null,
    });
  });

  it("removes only the queued pending entry when policy denies a send during an in-flight response", async () => {
    useChatStore.setState({
      conversationId: "conv_abc",
      abortController: new AbortController(),
      status: "streaming",
      sessionStatus: "running",
      activeResponse: { responseId: "resp_in_flight", state: "streaming", error: null },
    });
    fetchMock.mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith("/v1/sessions/conv_abc/events")) {
        return mockResponse({ queued: false, denied: true });
      }
      return defaultFetchHandler(input, init);
    });

    await useChatStore.getState().send("blocked queue item", "agent_xyz");

    const state = useChatStore.getState();
    // The denied queued message was never persisted, so its optimistic
    // bubble must disappear even though the previous response is still live.
    expect(state.pendingUserMessages).toEqual([]);
    // The denied branch of `send` only settles status when NOT already
    // streaming, so the POST-return path leaves the prior turn's
    // running/streaming state intact here.
    expect(state.status).toBe("streaming");
    expect(state.sessionStatus).toBe("running");
    expect(state.activeResponse).toEqual({
      responseId: "resp_in_flight",
      state: "streaming",
      error: null,
    });

    // The deny publishes no session-status pair at all: the agent never ran,
    // so there is no turn to report. The live turn streaming alongside it is
    // therefore untouched — no stray idle to fold its bubble, and no
    // client-side revive needed to undo one.
    expect(state.sessionStatus).toBe("running");
    expect(state.activeResponse).toEqual({
      responseId: "resp_in_flight",
      state: "streaming",
      error: null,
    });
  });

  it("finalizes a streaming turn on a bare idle edge (no response id)", () => {
    // Most idle publishes carry no response_id (the PTY-activity relay,
    // orchestration teardown). Leaving the turn "streaming" hid the
    // settled turn's "Worked for" fold and Fork action until a reload.
    useChatStore.setState({
      conversationId: "conv_abc",
      status: "streaming",
      sessionStatus: "running",
      activeResponse: { responseId: "resp_a", state: "streaming", error: null },
    });
    handleSessionEvent({
      type: "session_status",
      conversationId: "conv_abc",
      status: "idle",
    });
    const state = useChatStore.getState();
    expect(state.status).toBe("idle");
    expect(state.activeResponse).toEqual({
      responseId: "resp_a",
      state: "completed",
      error: null,
      completedAt: expect.any(Number),
    });
  });

  it("finalizes a streaming turn to failed on a bare failed edge", () => {
    useChatStore.setState({
      conversationId: "conv_abc",
      status: "streaming",
      sessionStatus: "running",
      activeResponse: { responseId: "resp_a", state: "streaming", error: null },
    });
    handleSessionEvent({
      type: "session_status",
      conversationId: "conv_abc",
      status: "failed",
    });
    expect(useChatStore.getState().activeResponse?.state).toBe("failed");
  });

  it("preserves a cancelled turn across a bare idle edge", () => {
    // The user's interrupt verdict must survive the trailing idle the
    // teardown publishes.
    useChatStore.setState({
      conversationId: "conv_abc",
      status: "idle",
      sessionStatus: "running",
      activeResponse: { responseId: "resp_a", state: "cancelled", error: null },
    });
    handleSessionEvent({
      type: "session_status",
      conversationId: "conv_abc",
      status: "idle",
    });
    expect(useChatStore.getState().activeResponse?.state).toBe("cancelled");
  });

  it("surfaces a failed send without settling the live turn", async () => {
    // A send that fails while a turn is streaming used to roll its optimistic
    // bubble back and stop there — no error block, no status change. That
    // silence is why a message that never reaches the agent looks like it was
    // never typed. The error must show; the live turn must not be touched.
    useChatStore.setState({
      conversationId: "conv_abc",
      boundAgentId: "agent_xyz",
      abortController: new AbortController(),
      status: "streaming",
      sessionStatus: "running",
      activeResponse: { responseId: "resp_in_flight", state: "streaming", error: null },
      blocks: [],
      pendingUserMessages: [],
    });
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/v1/sessions/conv_abc/events" && init?.method === "POST") {
        return Promise.reject(new Error("network down"));
      }
      return defaultFetchHandler(input, init);
    });

    await useChatStore.getState().send("does this vanish?", "agent_xyz");

    const state = useChatStore.getState();
    const errorBlocks = state.blocks.filter((b) => b.type === "error");
    expect(errorBlocks).toHaveLength(1);
    expect(errorBlocks[0]).toMatchObject({ type: "error", message: "network down" });
    // The optimistic bubble rolls back — no server record will reconcile it.
    expect(state.pendingUserMessages).toHaveLength(0);
    // The in-flight turn keeps its lifecycle: not failed, not settled.
    expect(state.activeResponse).toEqual({
      responseId: "resp_in_flight",
      state: "streaming",
      error: null,
    });
    expect(state.status).toBe("streaming");
  });

  it("isStaleCompletedResponse: only an old finalize is stale", () => {
    const base = { responseId: "r", error: null } as const;
    expect(
      isStaleCompletedResponse({
        activeResponse: { ...base, state: "completed", completedAt: Date.now() - 60_000 },
      }),
    ).toBe(true);
    expect(
      isStaleCompletedResponse({
        activeResponse: { ...base, state: "completed", completedAt: Date.now() - 1_000 },
      }),
    ).toBe(false);
    // No stamp (legacy snapshot) and non-completed states are never stale.
    expect(isStaleCompletedResponse({ activeResponse: { ...base, state: "completed" } })).toBe(
      false,
    );
    expect(
      isStaleCompletedResponse({
        activeResponse: { ...base, state: "streaming", completedAt: Date.now() - 60_000 },
      }),
    ).toBe(false);
    expect(isStaleCompletedResponse({ activeResponse: null })).toBe(false);
  });
});

describe("chatStore — background-shell tally (claude-native)", () => {
  it("adopts the shell count from a Stop-derived waiting status", () => {
    useChatStore.setState({ conversationId: "conv_abc", backgroundTaskCount: 0 });
    handleSessionEvent({
      type: "session_status",
      conversationId: "conv_abc",
      status: "waiting",
      backgroundTaskCount: 2,
    });
    const state = useChatStore.getState();
    expect(state.sessionStatus).toBe("waiting");
    expect(state.backgroundTaskCount).toBe(2);
  });

  it("keeps the sticky shell count when a trailing PTY idle lands", () => {
    // The claude-native turn-end sequence: the Stop hook publishes
    // waiting(+count), then ~1s later the PTY-activity watcher publishes a
    // bare `idle` once the pane quiesces. That trailing idle carries no
    // count and must NOT wipe the "N background tasks still running" signal — only
    // the status settles to idle.
    useChatStore.setState({
      conversationId: "conv_abc",
      sessionStatus: "waiting",
      backgroundTaskCount: 2,
      activeResponse: null,
    });
    handleSessionEvent({
      type: "session_status",
      conversationId: "conv_abc",
      status: "idle",
    });
    const state = useChatStore.getState();
    expect(state.sessionStatus).toBe("idle");
    expect(state.backgroundTaskCount).toBe(2);
  });

  it("clears the shell count on a hard failure", () => {
    useChatStore.setState({
      conversationId: "conv_abc",
      sessionStatus: "waiting",
      backgroundTaskCount: 2,
      activeResponse: null,
    });
    handleSessionEvent({
      type: "session_status",
      conversationId: "conv_abc",
      status: "failed",
    });
    expect(useChatStore.getState().backgroundTaskCount).toBe(0);
  });

  it("clears the shell count when a Stop hook reports zero remaining shells", () => {
    // The shell finished: the next turn-end Stop hook carries an authoritative
    // `background_task_count: 0` (an explicit count, not an absent field), which
    // must drop the "N background tasks still running" indicator. The earlier
    // sticky-on-idle behavior — which only existed to survive the countless
    // trailing PTY idle — must NOT swallow this authoritative zero.
    useChatStore.setState({
      conversationId: "conv_abc",
      sessionStatus: "waiting",
      backgroundTaskCount: 1,
      activeResponse: null,
    });
    handleSessionEvent({
      type: "session_status",
      conversationId: "conv_abc",
      status: "idle",
      backgroundTaskCount: 0,
    });
    const state = useChatStore.getState();
    expect(state.sessionStatus).toBe("idle");
    expect(state.backgroundTaskCount).toBe(0);
  });

  it("frees the local send lifecycle on a Stop-derived waiting edge (with responseId)", () => {
    // Regression: the claude/cursor-native Stop hook posts the turn-end
    // `waiting` edge WITH the ended turn's `response_id`. It must finalize the
    // local `status` to idle (the turn is done) so the composer sends the next
    // message instead of queuing it — while `sessionStatus` stays `waiting` and
    // the shell count sticks, keeping the "Working…" spinner lit.
    useChatStore.setState({
      conversationId: "conv_abc",
      status: "streaming",
      sessionStatus: "running",
      backgroundTaskCount: 0,
      activeResponse: { responseId: "resp_1", state: "streaming", error: null },
    });
    handleSessionEvent({
      type: "session_status",
      conversationId: "conv_abc",
      status: "waiting",
      responseId: "resp_1",
      backgroundTaskCount: 1,
    });
    const state = useChatStore.getState();
    expect(state.status).toBe("idle");
    expect(state.activeResponse?.state).toBe("completed");
    expect(state.sessionStatus).toBe("waiting");
    expect(state.backgroundTaskCount).toBe(1);
  });

  it("frees the local send lifecycle on a waiting edge whose id doesn't match", () => {
    // A `waiting` edge that carries no id (or a stale one) with no tracked
    // response must still free the send lifecycle so a message isn't stranded.
    useChatStore.setState({
      conversationId: "conv_abc",
      status: "streaming",
      sessionStatus: "running",
      backgroundTaskCount: 0,
      activeResponse: { responseId: "resp_1", state: "streaming", error: null },
    });
    handleSessionEvent({
      type: "session_status",
      conversationId: "conv_abc",
      status: "waiting",
      backgroundTaskCount: 1,
    });
    const state = useChatStore.getState();
    expect(state.status).toBe("idle");
    expect(state.sessionStatus).toBe("waiting");
    // The stale streaming bubble is finalized so it doesn't linger spinning —
    // no future edge names this id to close it.
    expect(state.activeResponse?.state).toBe("completed");
  });

  it("keeps the sticky shell count when a new turn starts (running edge)", () => {
    // A `running` edge with no count means a fresh turn began, but shells
    // launched earlier keep running across the turn boundary — so the tally
    // persists and the composer pill stays lit alongside the "Working…"
    // shimmer. (The PTY `running` carries no count; the next Stop hook
    // re-reports authoritatively.) Mirrors the server's `_publish_status`.
    useChatStore.setState({
      conversationId: "conv_abc",
      sessionStatus: "idle",
      backgroundTaskCount: 2,
      activeResponse: null,
    });
    handleSessionEvent({
      type: "session_status",
      conversationId: "conv_abc",
      status: "running",
    });
    const state = useChatStore.getState();
    expect(state.sessionStatus).toBe("running");
    expect(state.backgroundTaskCount).toBe(2);
  });

  it("keeps the sticky shell count when the user sends a new turn", async () => {
    // Asking another question while shells run must not blink the pill off: the
    // shells keep running across the new turn, so the tally persists and the
    // pill stays up alongside the "Working…" shimmer. The next Stop hook
    // re-reports it authoritatively.
    seedSession("conv_abc");
    useChatStore.setState({ conversationId: "conv_abc", status: "idle" });
    // Bind the stream first (a real, open session the user is looking at) so the
    // follow-up send below takes the already-bound path and does NOT re-hydrate
    // the count from the snapshot.
    await useChatStore.getState().send("first question", "agent_xyz");
    // A prior turn ended with a background shell still running.
    useChatStore.setState({
      sessionStatus: "waiting",
      backgroundTaskCount: 2,
      status: "idle",
      activeResponse: null,
    });
    await useChatStore.getState().send("another question", "agent_xyz");
    expect(useChatStore.getState().backgroundTaskCount).toBe(2);
  });
});

describe("chatStore — send (cross-session routing)", () => {
  it("delivers a queued send to the session it was composed in, not the now-active session", async () => {
    // Repro for the cross-session message-routing leak.
    //
    // Session B's host is online but its runner is not started (daemon
    // restart), so B's first message POST blocks server-side while the
    // server relaunches a runner. A second message typed into B queues
    // behind the first on the module-level send chain. While the chain is
    // stalled the user navigates to a different, already-running session A.
    //
    // The queued second message MUST still be delivered to session B — the
    // session it was composed in. The bug routed it to A because the POST
    // target was re-resolved from the live `conversationId` only AFTER the
    // chain unblocked, by which point the user had switched to A.
    seedSession("conv_a"); // the running session the user switches to
    useChatStore.setState({
      conversationId: "conv_b",
      abortController: new AbortController(),
      status: "idle",
    });

    // Capture every /events POST (url + message text). Hold B's FIRST
    // /events POST open to model the no-runner relaunch wait; that open
    // POST is what keeps the send chain stalled while the user switches.
    interface EventPost {
      sessionId: string;
      text: string;
    }
    const eventPosts: EventPost[] = [];
    let releaseFirstPost: () => void = () => {};
    let firstPostHeld = false;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const match = url.match(/^\/v1\/sessions\/([^/]+)\/events$/);
      if (match && init?.method === "POST") {
        const text = (JSON.parse(init.body as string).data.content[0] as { text: string }).text;
        eventPosts.push({ sessionId: match[1]!, text });
        if (!firstPostHeld) {
          firstPostHeld = true;
          return new Promise<Response>((resolve) => {
            releaseFirstPost = () => resolve(mockResponse({ queued: true, item_id: "ci_b1" }));
          });
        }
        return mockResponse({ queued: true, item_id: "ci_mock" });
      }
      return defaultFetchHandler(input, init);
    });

    // Two messages typed into B, no await — msg2 queues behind msg1.
    const p1 = useChatStore.getState().send("msg1", "agent_xyz");
    const p2 = useChatStore.getState().send("msg2", "agent_xyz");
    await tick();

    // Only msg1 is in flight (held open); msg2 is parked on the chain. If
    // both had fired, serialization is broken and the repro is invalid.
    expect(eventPosts).toEqual([{ sessionId: "conv_b", text: "msg1" }]);

    // User navigates to the running session while B's first POST is stalled.
    await useChatStore.getState().switchTo("conv_a");
    expect(useChatStore.getState().conversationId).toBe("conv_a");

    // B's first message finally lands → the chain releases msg2.
    releaseFirstPost();
    await Promise.all([p1, p2]);

    // msg2 was composed in B and must be delivered to B's events endpoint,
    // never to the now-active session A. Failure here (sessionId "conv_a")
    // is the cross-session leak: a message meant for B injected into A.
    const msg2 = eventPosts.find((p) => p.text === "msg2");
    expect(msg2).toBeDefined();
    expect(msg2!.sessionId).toBe("conv_b");

    // And A received nothing — no stray message leaked into the running session.
    expect(eventPosts.filter((p) => p.sessionId === "conv_a")).toEqual([]);
  });

  it("an in-flight send that fails after a switch does not clobber the now-active session", async () => {
    // Companion to the routing fix (Copilot review): a send to session B can
    // still be in flight when the user switches to A. If B's POST then fails,
    // its catch must NOT reset A's status/activeResponse. The vehicle is a
    // *first* send (B idle → alreadyStreaming=false), so the catch's
    // status-reset branch is live; a queued send would have alreadyStreaming
    // =true and skip it, masking the bug.
    seedSession("conv_a");
    useChatStore.setState({
      conversationId: "conv_b",
      abortController: new AbortController(),
      status: "idle",
    });

    // Hold B's POST open so it is still in flight across the switch, then
    // resolve it as a 500 so postEvent throws into the catch.
    let failPost: () => void = () => {};
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/v1/sessions/conv_b/events" && init?.method === "POST") {
        return new Promise<Response>((resolve) => {
          failPost = () => resolve(mockResponse({}, { ok: false, status: 500 }));
        });
      }
      return defaultFetchHandler(input, init);
    });

    const p1 = useChatStore.getState().send("msg1", "agent_xyz");
    await tick();

    // Switch to A, then simulate A having its own in-flight streaming turn.
    await useChatStore.getState().switchTo("conv_a");
    const aResponse = { responseId: "resp_a", state: "streaming" as const, error: null };
    useChatStore.setState({
      status: "streaming",
      sessionStatus: "running",
      activeResponse: aResponse,
    });

    // B's POST fails (500) → postEvent throws → send's catch runs.
    failPost();
    await p1;

    const state = useChatStore.getState();
    // A's UI is untouched by B's failed send. Without the catch-block guard,
    // finalizeActive("failed") + set({status:"idle"}) would run against the
    // active store (A), flipping status to "idle" and activeResponse.state to
    // "failed".
    expect(state.conversationId).toBe("conv_a");
    expect(state.status).toBe("streaming");
    expect(state.sessionStatus).toBe("running");
    expect(state.activeResponse).toEqual(aResponse);
  });
});

describe("chatStore — send (file attachments)", () => {
  it("refreshes the pending entry with real file_ids after upload", async () => {
    // Claude-native's session.input.consumed is text-only, so the
    // consumed handler merges pending file blocks with server text;
    // pending must already carry real ids or the bubble renders the
    // chip forever.
    useChatStore.setState({
      conversationId: "conv_existing",
      abortController: new AbortController(),
    });

    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/v1/sessions/conv_existing/resources/files")) {
        return mockResponse({
          id: "file_real_abc123",
          name: "photo.png",
          metadata: { filename: "photo.png", bytes: 10, created_at: 0 },
        });
      }
      return defaultFetchHandler(input, init);
    });

    const file = new File(["fake-bytes"], "photo.png", { type: "image/png" });
    await useChatStore.getState().send("look at this", "agent_xyz", [file]);

    const state = useChatStore.getState();
    expect(state.pendingUserMessages).toHaveLength(1);
    // Real upload id, not "pending:photo.png".
    expect(state.pendingUserMessages[0]!.content).toEqual([
      { type: "input_image", file_id: "file_real_abc123", filename: "photo.png" },
      { type: "input_text", text: "look at this" },
    ]);
  });

  it("preserves the real file_id through a text-only consumed event", async () => {
    // End-to-end claude-native: upload → text-only consumed event →
    // promoted bubble must carry the real id so UserBubble takes the
    // <img> branch instead of the chip.
    useChatStore.setState({
      conversationId: "conv_existing",
      abortController: new AbortController(),
    });

    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/v1/sessions/conv_existing/resources/files")) {
        return mockResponse({
          id: "file_real_xyz789",
          name: "screenshot.png",
          metadata: { filename: "screenshot.png", bytes: 10, created_at: 0 },
        });
      }
      return defaultFetchHandler(input, init);
    });

    const file = new File(["bytes"], "screenshot.png", { type: "image/png" });
    await useChatStore.getState().send("whats going on", "agent_xyz", [file]);

    handleSessionEvent({
      type: "session_input_consumed",
      itemId: "msg_persisted_1",
      itemType: "message",
      data: {
        role: "user",
        content: [
          {
            type: "input_text",
            text: "[Attached: /tmp/uploads/screenshot.png]\n\nwhats going on",
          },
        ],
      },
    });

    const state = useChatStore.getState();
    expect(state.pendingUserMessages).toEqual([]);
    const promoted = state.blocks[0] as UserMessageBlock;
    expect(promoted.type).toBe("user_message");
    expect(promoted.ctx.itemId).toBe("msg_persisted_1");
    // input_text still carries "[Attached: ...]"; ChatPage strips it
    // via ATTACHED_RE at render time.
    expect(promoted.content).toEqual([
      { type: "input_image", file_id: "file_real_xyz789", filename: "screenshot.png" },
      {
        type: "input_text",
        text: "[Attached: /tmp/uploads/screenshot.png]\n\nwhats going on",
      },
    ]);
  });

  it("promotes real file_ids on the sending conversation when the upload outlives a switch", async () => {
    // The upload can be slow, so the user may background the conversation while
    // it is in flight. The refresh must target the conversation that SENT the
    // attachment: resolving it late from the visible conversation left the
    // sender's bubble on `pending:<filename>` ids, and claude-native's text-only
    // `session.input.consumed` then falls back to those placeholders — committing
    // an attachment the server never gave an id.
    seedSession("conv_upload", []);
    seedSession("conv_visible", []);
    let releaseUpload: () => void = () => {};
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/v1/sessions/conv_upload/resources/files")) {
        return new Promise<Response>((resolve) => {
          releaseUpload = () =>
            resolve(
              mockResponse({
                id: "file_real_bg",
                name: "bg.png",
                metadata: { filename: "bg.png", bytes: 10, created_at: 0 },
              }),
            );
        });
      }
      return defaultFetchHandler(input, init);
    });

    await useChatStore.getState().switchTo("conv_upload");
    const file = new File(["bytes"], "bg.png", { type: "image/png" });
    const sending = useChatStore.getState().send("with a picture", "agent_xyz", [file]);
    await tick();

    // Navigate away while the upload is still open.
    await useChatStore.getState().switchTo("conv_visible");
    releaseUpload();
    await sending;

    // The sender's bubble carries the real id...
    const sender = conversationRegistry.peek("conv_upload")!.getState();
    expect(sender.pendingUserMessages[0]!.content).toEqual([
      { type: "input_image", file_id: "file_real_bg", filename: "bg.png" },
      { type: "input_text", text: "with a picture" },
    ]);
    // ...and the conversation the user is looking at was never touched.
    expect(useChatStore.getState().pendingUserMessages).toEqual([]);
  });

  it("does not hand the upload to the interrupt marker that precedes it", async () => {
    // Steering mid-tool-use makes Claude write its own "[Request interrupted
    // by user...]" record BEFORE the steering message, and the forwarder
    // mirrors both back as consumed events. The marker was never queued
    // here, so the FIFO-head fallback must not pop the optimistic bubble for
    // it — otherwise the marker renders as raw user text beside the
    // screenshots and the real message lands as a blank bubble.
    useChatStore.setState({
      conversationId: "conv_existing",
      abortController: new AbortController(),
    });

    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/v1/sessions/conv_existing/resources/files")) {
        return mockResponse({
          id: "file_real_shot",
          name: "shot.png",
          metadata: { filename: "shot.png", bytes: 10, created_at: 0 },
        });
      }
      return defaultFetchHandler(input, init);
    });

    const file = new File(["bytes"], "shot.png", { type: "image/png" });
    await useChatStore.getState().send("", "agent_xyz", [file]);

    handleSessionEvent({
      type: "session_input_consumed",
      itemId: "msg_interrupt",
      itemType: "message",
      data: {
        role: "user",
        content: [{ type: "input_text", text: "[Request interrupted by user for tool use]" }],
      },
    });

    // Marker committed bare, optimistic bubble untouched.
    const afterMarker = useChatStore.getState();
    expect(afterMarker.pendingUserMessages).toHaveLength(1);
    const marker = afterMarker.blocks[0] as UserMessageBlock;
    expect(marker.content).toEqual([
      { type: "input_text", text: "[Request interrupted by user for tool use]" },
    ]);

    handleSessionEvent({
      type: "session_input_consumed",
      itemId: "msg_steer",
      itemType: "message",
      data: {
        role: "user",
        content: [{ type: "input_text", text: "[Attached: /tmp/uploads/shot.png]" }],
      },
    });

    // The upload landed on the message that actually queued it.
    const state = useChatStore.getState();
    expect(state.pendingUserMessages).toEqual([]);
    const steered = state.blocks[1] as UserMessageBlock;
    expect(steered.ctx.itemId).toBe("msg_steer");
    expect(steered.content).toEqual([
      { type: "input_image", file_id: "file_real_shot", filename: "shot.png" },
      { type: "input_text", text: "[Attached: /tmp/uploads/shot.png]" },
    ]);
  });

  it("keeps the optimistic bubble's stable key on the native path (no pending_id adoption)", async () => {
    // The native POST returns a pending_id, but the optimistic bubble
    // must KEEP its client temp id as its React key — swapping to the
    // server id mid-send remounts the bubble (a visible flink). The
    // image (real upload id) stays on the entry regardless.
    useChatStore.setState({
      conversationId: "conv_existing",
      abortController: new AbortController(),
    });

    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/v1/sessions/conv_existing/resources/files")) {
        return mockResponse({
          id: "file_real_native",
          name: "diagram.png",
          metadata: { filename: "diagram.png", bytes: 10, created_at: 0 },
        });
      }
      // Native message POST hands back a pending_id (no item_id).
      if (url.endsWith("/v1/sessions/conv_existing/events")) {
        return mockResponse({ queued: true, pending_id: "pending_native_1" });
      }
      return defaultFetchHandler(input, init);
    });

    const file = new File(["bytes"], "diagram.png", { type: "image/png" });
    await useChatStore.getState().send("draw this", "agent_xyz", [file]);

    // The bubble keeps its client temp id (stable key) — NOT the server
    // pending id — and carries the real-id image.
    const afterSend = useChatStore.getState();
    expect(afterSend.pendingUserMessages).toHaveLength(1);
    expect(afterSend.pendingUserMessages[0]!.tempId).toMatch(/^pend_/);
    expect(afterSend.pendingUserMessages[0]!.content).toEqual([
      { type: "input_image", file_id: "file_real_native", filename: "diagram.png" },
      { type: "input_text", text: "draw this" },
    ]);

    // Text-only consume (transcript round-trip). clearedPendingId names
    // the server id, which the optimistic bubble does not carry, so the
    // FIFO head path promotes it — and still merges the image.
    handleSessionEvent({
      type: "session_input_consumed",
      itemId: "msg_native_1",
      itemType: "message",
      clearedPendingId: "pending_native_1",
      data: {
        role: "user",
        content: [{ type: "input_text", text: "[Attached: /tmp/diagram.png]\n\ndraw this" }],
      },
    });

    const state = useChatStore.getState();
    expect(state.pendingUserMessages).toEqual([]);
    const promoted = state.blocks[0] as UserMessageBlock;
    // The image survived the promotion — merged ahead of the text-only
    // transcript content.
    expect(promoted.content).toEqual([
      { type: "input_image", file_id: "file_real_native", filename: "diagram.png" },
      { type: "input_text", text: "[Attached: /tmp/diagram.png]\n\ndraw this" },
    ]);
  });

  it("reuses a prior upload when re-sending the same File after a failed post", async () => {
    // send()'s first attempt uploads then fails at the post; the caller retries
    // with the same File. The upload must not run twice (which would orphan the
    // first blob) — the second send reuses the cached file_id.
    useChatStore.setState({
      conversationId: "conv_existing",
      abortController: new AbortController(),
    });
    let uploads = 0;
    let failPost = true;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/v1/sessions/conv_existing/resources/files")) {
        uploads += 1;
        return mockResponse({
          id: "file_dedupe_send",
          name: "photo.png",
          metadata: { filename: "photo.png", bytes: 10, created_at: 0 },
        });
      }
      if (url.endsWith("/v1/sessions/conv_existing/events") && init?.method === "POST") {
        if (failPost) return mockResponse({}, { ok: false, status: 500 });
      }
      return defaultFetchHandler(input, init);
    });

    const file = new File(["fake-bytes"], "photo.png", { type: "image/png" });
    await useChatStore.getState().send("look at this", "agent_xyz", [file]);
    expect(uploads).toBe(1);

    // Retry with the SAME File object → cached upload is reused.
    failPost = false;
    await useChatStore.getState().send("look at this", "agent_xyz", [file]);
    expect(uploads).toBe(1);
  });

  it("hands the message back for retry when the upload is rejected", async () => {
    // A 415 on an unsupported attachment must not swallow the typed text: the
    // optimistic bubble rolls back, so `failedSendDraft` is the only thing
    // left holding it. The banner carries the server's reason, not "415".
    useChatStore.setState({
      conversationId: "conv_existing",
      abortController: new AbortController(),
    });
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/v1/sessions/conv_existing/resources/files")) {
        return mockResponse(
          {
            detail:
              "Unsupported attachment type 'application/zip'. Only images, " +
              "PDF, and text/code files can be attached.",
          },
          { ok: false, status: 415 },
        );
      }
      return defaultFetchHandler(input, init);
    });

    const zip = new File([new Uint8Array(4)], "photos.zip", { type: "application/zip" });
    await useChatStore.getState().send("summarize these photos", "agent_xyz", [zip]);

    const state = useChatStore.getState();
    // Optimistic bubble rolled back — the send never reached the server.
    expect(state.pendingUserMessages).toEqual([]);
    expect(state.failedSendDraft).toEqual({
      conversationId: "conv_existing",
      text: "summarize these photos",
      files: [zip],
    });
    const error = state.blocks.at(-1) as { type: string; message: string };
    expect(error.type).toBe("error");
    expect(error.message).toContain("Unsupported attachment type 'application/zip'");
  });
});

describe("chatStore — stop", () => {
  it("posts {type: 'interrupt'} to the events endpoint without aborting the local stream", async () => {
    const controller = new AbortController();
    useChatStore.setState({
      conversationId: "conv_abc",
      abortController: controller,
    });

    useChatStore.getState().stop();
    await tick();

    const events = fetchMock.mock.calls.filter(([u]) =>
      String(u).endsWith("/v1/sessions/conv_abc/events"),
    );
    expect(events).toHaveLength(1);
    const body = JSON.parse((events[0]![1] as RequestInit).body as string);
    expect(body).toEqual({ type: "interrupt", data: {} });
    // Local stream stays open after stop — only switchTo / unload
    // tears it down.
    expect(controller.signal.aborted).toBe(false);
  });

  it("clears local working state immediately while the interrupt ack is pending", () => {
    useChatStore.setState({
      conversationId: "conv_abc",
      pendingUserMessages: [
        {
          tempId: "pend_1",
          content: [{ type: "input_text", text: "stop me" }],
        },
      ],
      activeResponse: { responseId: "resp_1", state: "streaming", error: null },
      status: "streaming",
      sessionStatus: "running",
    });
    seedConversationsCache([conv("conv_abc", "running")]);

    useChatStore.getState().stop();

    const state = useChatStore.getState();
    expect(state.pendingUserMessages).toEqual([]);
    expect(state.status).toBe("idle");
    expect(state.sessionStatus).toBe("idle");
    expect(state.activeResponse).toEqual({
      responseId: "resp_1",
      state: "cancelled",
      error: null,
    });
    expect(readConversationRows()[0]?.status).toBe("idle");
  });

  it("leaves a non-streaming activeResponse untouched on stop", () => {
    // Pins the `state === "streaming"` guard: stop() still clears the working
    // state, but must NOT overwrite a non-streaming activeResponse (dropping the
    // guard would clobber a completed response with a cancelled decoration).
    useChatStore.setState({
      conversationId: "conv_abc",
      pendingUserMessages: [
        { tempId: "pend_1", content: [{ type: "input_text", text: "stop me" }] },
      ],
      activeResponse: { responseId: "resp_1", state: "completed", error: null },
      status: "streaming",
      sessionStatus: "running",
    });
    seedConversationsCache([conv("conv_abc", "running")]);

    useChatStore.getState().stop();

    const state = useChatStore.getState();
    expect(state.pendingUserMessages).toEqual([]);
    expect(state.status).toBe("idle");
    expect(state.sessionStatus).toBe("idle");
    // Untouched — the guard skipped the cancelled overwrite.
    expect(state.activeResponse).toEqual({
      responseId: "resp_1",
      state: "completed",
      error: null,
    });
    expect(readConversationRows()[0]?.status).toBe("idle");
  });

  it("no-op when no session is bound", () => {
    useChatStore.getState().stop();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("chatStore — handleSessionEvent (session.* events)", () => {
  // These cases call `handleSessionEvent` directly, without the stream that
  // normally names the delivering conversation. Bind `conv_abc` — the id their
  // events carry — so a write lands on the active conversation exactly as it
  // would in production. Cases that assert the cross-conversation guard
  // deliberately bind something else, or name another conversation in the event.
  beforeEach(() => {
    useChatStore.setState({ conversationId: "conv_abc" });
  });

  describe("session.presence", () => {
    it("replaces the viewer list wholesale for the bound conversation", () => {
      useChatStore.setState({
        conversationId: "conv_abc",
        viewers: [{ userId: "stale@example.com", idle: false }],
      });
      handleSessionEvent({
        type: "session_presence",
        conversationId: "conv_abc",
        viewers: [
          { userId: "alice@example.com", joinedAt: "2026-06-10T17:00:00Z", idle: false },
          { userId: "bob@example.com", idle: true },
        ],
      });
      // Full-state protocol: the stale viewer must be GONE, not merged —
      // a merge here means leave events can never clear anyone.
      expect(useChatStore.getState().viewers).toEqual([
        { userId: "alice@example.com", joinedAt: "2026-06-10T17:00:00Z", idle: false },
        { userId: "bob@example.com", idle: true },
      ]);
    });

    it("ignores a presence frame from a switched-away conversation", () => {
      useChatStore.setState({
        conversationId: "conv_current",
        viewers: [],
      });
      handleSessionEvent({
        type: "session_presence",
        conversationId: "conv_other",
        viewers: [{ userId: "alice@example.com", idle: false }],
      });
      // A late frame from the previous session's still-draining stream
      // must not paint the new session's header with foreign viewers.
      expect(useChatStore.getState().viewers).toEqual([]);
    });
  });

  describe("session.superseded", () => {
    it("records the redirect target for the bound conversation", () => {
      useChatStore.setState({ conversationId: "conv_old", redirectToConversationId: null });
      handleSessionEvent({
        type: "session_superseded",
        conversationId: "conv_old",
        targetConversationId: "conv_new",
        reason: "clear",
      });
      expect(useChatStore.getState().redirectToConversationId).toBe("conv_new");
    });

    it("clears the superseded conversation's lingering optimistic bubble", () => {
      useChatStore.setState({
        conversationId: "conv_old",
        redirectToConversationId: null,
        pendingUserMessages: [
          { tempId: "pend_clear", content: [{ type: "input_text", text: "/clear" }] },
        ],
      });
      handleSessionEvent({
        type: "session_superseded",
        conversationId: "conv_old",
        targetConversationId: "conv_new",
        reason: "clear",
      });
      const state = useChatStore.getState();
      // The `/clear` never gets a session.input.consumed on conv_old (the
      // runner rotated away), so its bubble must be dropped here rather than
      // spinning forever.
      expect(state.pendingUserMessages).toEqual([]);
    });

    it("ignores a superseded frame from a switched-away conversation", () => {
      useChatStore.setState({ conversationId: "conv_current", redirectToConversationId: null });
      handleSessionEvent({
        type: "session_superseded",
        conversationId: "conv_other",
        targetConversationId: "conv_new",
        reason: "clear",
      });
      // A late frame from the previous session's still-draining stream must
      // not yank the user out of the conversation they're now viewing.
      expect(useChatStore.getState().redirectToConversationId).toBeNull();
    });

    it("ignores a self-target no-op", () => {
      useChatStore.setState({ conversationId: "conv_old", redirectToConversationId: null });
      handleSessionEvent({
        type: "session_superseded",
        conversationId: "conv_old",
        targetConversationId: "conv_old",
        reason: "clear",
      });
      expect(useChatStore.getState().redirectToConversationId).toBeNull();
    });
  });

  describe("session.status", () => {
    it("updates sessionStatus from the event", () => {
      const event: SessionStatusEvent = {
        type: "session_status",
        conversationId: "conv_abc",
        status: "running",
      };
      handleSessionEvent(event);
      expect(useChatStore.getState().sessionStatus).toBe("running");
    });

    it("accepts the live-only 'waiting' state (snapshot can't carry it)", () => {
      handleSessionEvent({
        type: "session_status",
        conversationId: "conv_abc",
        status: "waiting",
      });
      expect(useChatStore.getState().sessionStatus).toBe("waiting");
    });

    it("surfaces one terminal error per native response", () => {
      useChatStore.setState({ blocks: [] });
      const error = {
        code: "codex_turn_error",
        message: "You've hit your usage limit.",
      };

      handleSessionEvent({
        type: "session_status",
        conversationId: "conv_abc",
        status: "failed",
        responseId: "codex_turn_1",
        error,
      });
      handleSessionEvent({
        type: "session_status",
        conversationId: "conv_abc",
        status: "failed",
        responseId: "codex_turn_1",
        error,
      });
      handleSessionEvent({
        type: "session_status",
        conversationId: "conv_abc",
        status: "failed",
        responseId: "codex_turn_2",
        error,
      });

      const errors = useChatStore.getState().blocks.filter((block) => block.type === "error");
      expect(errors).toHaveLength(2);
      expect(errors.map((block) => block.ctx.responseId)).toEqual(["codex_turn_1", "codex_turn_2"]);
    });

    it("idle clears local streaming when no active response will send response_end", () => {
      useChatStore.setState({
        status: "streaming",
        activeResponse: null,
        pendingUserMessages: [
          { tempId: "pend_policy_denied", content: [{ type: "input_text", text: "blocked" }] },
        ],
      });

      handleSessionEvent({
        type: "session_status",
        conversationId: "conv_abc",
        status: "idle",
      });

      const state = useChatStore.getState();
      // INPUT policy DENY publishes running/idle without response_end or
      // session.input.consumed. Terminal status must therefore settle the
      // local streaming flag and drop the dangling optimistic bubble when
      // no active response exists.
      expect(state.status).toBe("idle");
      expect(state.pendingUserMessages).toEqual([]);
    });

    it("idle does NOT clear the optimistic bubble on a native-terminal session", () => {
      // Native (claude/codex-native) web messages aren't persisted at POST
      // time — they round-trip through the vendor TUI and reconcile via the
      // transcript forwarder's session.input.consumed event, which can arrive
      // AFTER a transient idle/failed (Claude cold-start on resume, runner
      // relaunch churn). Clearing the bubble here would drop it before its
      // consumed event lands, leaving a multi-second gap until the committed
      // item re-renders (the host-restart "bubble disappears" bug). The
      // consumed handler (+ server-side pending_inputs TTL) is the authority.
      useChatStore.setState({
        status: "streaming",
        activeResponse: null,
        isNativeTerminalSession: true,
        pendingUserMessages: [
          { tempId: "pend_native", content: [{ type: "input_text", text: "hi" }] },
        ],
      });

      handleSessionEvent({
        type: "session_status",
        conversationId: "conv_abc",
        status: "idle",
      });

      const state = useChatStore.getState();
      // sessionStatus still tracks the event and local streaming still settles.
      expect(state.sessionStatus).toBe("idle");
      expect(state.status).toBe("idle");
      // But the optimistic bubble survives for the lagging consumed event —
      // reverting the native guard makes this []: the regression under test.
      expect(state.pendingUserMessages).toEqual([
        { tempId: "pend_native", content: [{ type: "input_text", text: "hi" }] },
      ]);
    });

    it("idle settles sessionStatus AND finalizes a still-streaming bubble", () => {
      useChatStore.setState({
        status: "streaming",
        sessionStatus: "running",
        activeResponse: { responseId: "resp_live", state: "streaming", error: null },
      });

      handleSessionEvent({
        type: "session_status",
        conversationId: "conv_abc",
        status: "idle",
      });

      const state = useChatStore.getState();
      // sessionStatus tracks the server's session-level status 1:1 — a server
      // idle means idle, and the "Working…" indicator (which reads only
      // sessionStatus) must turn off. The bubble lifecycle settles on the
      // same edge: a bare (id-less) idle is the NORMAL turn-end shape for the
      // status file and orchestration teardown, and leaving the turn
      // "streaming" hid its "Worked for" fold and Fork action until a reload.
      // Every idle that reaches here is a real turn end — control signals no
      // longer publish status.
      expect(state.sessionStatus).toBe("idle");
      expect(state.status).toBe("idle");
      expect(state.activeResponse).toEqual({
        responseId: "resp_live",
        state: "completed",
        error: null,
        completedAt: expect.any(Number),
      });
    });

    it("idle refetches the parent's child list when the conversation is a child", () => {
      // The child's cached snapshot identifies its parent; a finished turn
      // must refetch the parent's child list so the rail row's preview is
      // current (the runner can't supply a claude-native reply preview).
      client.setQueryData(["session", "conv_child1"], { parentSessionId: "conv_parent" });
      const spy = vi.spyOn(client, "invalidateQueries");
      handleSessionEvent({
        type: "session_status",
        conversationId: "conv_child1",
        status: "idle",
      });
      expect(spy).toHaveBeenCalledWith({ queryKey: childSessionsQueryKey("conv_parent") });
    });

    it("idle refetches a root session's own snapshot (feeds the main-row preview)", () => {
      client.setQueryData(["session", "conv_root"], { parentSessionId: null });
      const spy = vi.spyOn(client, "invalidateQueries");
      handleSessionEvent({
        type: "session_status",
        conversationId: "conv_root",
        status: "idle",
      });
      expect(spy).toHaveBeenCalledWith({ queryKey: ["session", "conv_root"] });
    });

    it("running with a NEW responseId refetches the snapshot once (turn start)", () => {
      // The runner persists turn-scoped labels (the cost advisor's
      // cost_control.plan verdict) BEFORE the harness runs; this refetch is
      // what lets the routing-verdict tooltip render mid-turn instead of
      // only after the idle/failed turn-end invalidation.
      //
      // Bind conv_ts: the once-per-turn latch compares against
      // `activeResponse.responseId`, which only advances when the status patch
      // applies — i.e. for the conversation on screen.
      useChatStore.setState({ conversationId: "conv_ts" });
      const spy = vi.spyOn(client, "invalidateQueries");
      handleSessionEvent({
        type: "session_status",
        conversationId: "conv_ts",
        status: "running",
        responseId: "resp_t1",
      });
      // exact:true targets the snapshot only — the heavier
      // ["session", id, "items", ...] queries must not refetch mid-turn.
      expect(spy).toHaveBeenCalledExactlyOnceWith({
        queryKey: ["session", "conv_ts"],
        exact: true,
      });
      // Same-turn status churn (waiting park, duplicate running tick)
      // repeats the responseId — re-invalidating here would spam a
      // snapshot fetch per status event, not once per turn.
      handleSessionEvent({
        type: "session_status",
        conversationId: "conv_ts",
        status: "waiting",
        responseId: "resp_t1",
      });
      handleSessionEvent({
        type: "session_status",
        conversationId: "conv_ts",
        status: "running",
        responseId: "resp_t1",
      });
      expect(spy).toHaveBeenCalledTimes(1);
    });

    it("turn-start refetch fires per turn and leaves the turn-end refetch unchanged", () => {
      client.setQueryData(["session", "conv_cycle"], { parentSessionId: null });
      const spy = vi.spyOn(client, "invalidateQueries");
      handleSessionEvent({
        type: "session_status",
        conversationId: "conv_cycle",
        status: "running",
        responseId: "resp_c1",
      });
      handleSessionEvent({
        type: "session_status",
        conversationId: "conv_cycle",
        status: "idle",
        responseId: "resp_c1",
      });
      // One full turn = exactly two snapshot refetches: the turn-start one
      // (exact) plus the pre-existing turn-end preview refresh (prefix).
      // A third call means the turn-start path double-fired; a single call
      // means one of the two boundaries regressed.
      expect(spy.mock.calls).toEqual([
        [{ queryKey: ["session", "conv_cycle"], exact: true }],
        [{ queryKey: ["session", "conv_cycle"] }],
      ]);
      // The NEXT turn carries a fresh responseId, so its start must
      // refetch again — the once-per-turn guard is per turn, not per session.
      handleSessionEvent({
        type: "session_status",
        conversationId: "conv_cycle",
        status: "running",
        responseId: "resp_c2",
      });
      expect(spy).toHaveBeenCalledTimes(3);
      expect(spy).toHaveBeenLastCalledWith({
        queryKey: ["session", "conv_cycle"],
        exact: true,
      });
    });

    it("running WITHOUT a responseId does not refetch the snapshot", () => {
      // A prior turn left an activeResponse behind: without the explicit
      // undefined guard, every id-less running tick would compare
      // undefined !== "resp_prev" and refetch on each status event.
      useChatStore.setState({
        activeResponse: { responseId: "resp_prev", state: "streaming", error: null },
      });
      const spy = vi.spyOn(client, "invalidateQueries");
      handleSessionEvent({
        type: "session_status",
        conversationId: "conv_anon",
        status: "running",
      });
      // No responseId = no turn identity to dedupe on, so no refetch.
      expect(spy).not.toHaveBeenCalled();
    });

    it("patches the active session's sidebar row in lockstep — no 4 s poll lag", () => {
      // The badge reads conversation.status from the polled list; without this
      // patch it lagged the chat's live "Working…" by up to one poll (4 s).
      seedConversationsCache([conv("conv_abc", "idle"), conv("conv_other", "idle")]);
      handleSessionEvent({ type: "session_status", conversationId: "conv_abc", status: "running" });
      const rows = readConversationRows();
      expect(rows.find((c) => c.id === "conv_abc")?.status).toBe("running");
      // Only the streamed (active) session's row moves; others wait for the poll.
      expect(rows.find((c) => c.id === "conv_other")?.status).toBe("idle");
    });

    it("collapses live 'waiting' to the list's 'running' (server-list parity)", () => {
      // GET /v1/sessions maps running/waiting → "running"; the live patch must
      // match so the badge agrees with the next poll instead of flapping.
      seedConversationsCache([conv("conv_abc", "idle")]);
      handleSessionEvent({ type: "session_status", conversationId: "conv_abc", status: "waiting" });
      expect(readConversationRows().find((c) => c.id === "conv_abc")?.status).toBe("running");
    });

    it("maps 'failed'→'failed' and 'idle'→'idle' in the sidebar row", () => {
      seedConversationsCache([conv("conv_abc", "running")]);
      handleSessionEvent({ type: "session_status", conversationId: "conv_abc", status: "failed" });
      expect(readConversationRows().find((c) => c.id === "conv_abc")?.status).toBe("failed");
      handleSessionEvent({ type: "session_status", conversationId: "conv_abc", status: "idle" });
      expect(readConversationRows().find((c) => c.id === "conv_abc")?.status).toBe("idle");
    });

    it("drops the sidebar row to 'idle' while background shells outlive the turn", () => {
      // The claude-native turn settles to idle while shells keep running. The
      // session takes a new message right away, so the row must NOT keep
      // spinning — only the in-chat indicator reports the shells (via the
      // tally, which stays put for it).
      seedConversationsCache([conv("conv_abc", "running")]);
      useChatStore.setState({ conversationId: "conv_abc", backgroundTaskCount: 1 });
      handleSessionEvent({ type: "session_status", conversationId: "conv_abc", status: "idle" });
      expect(readConversationRows().find((c) => c.id === "conv_abc")?.status).toBe("idle");
      expect(useChatStore.getState().backgroundTaskCount).toBe(1);
    });

    it("drops the sidebar row to 'idle' once the last background shell finishes", () => {
      // The finishing Stop hook carries an authoritative `background_task_count: 0`,
      // which clears the tally — the sidebar spinner must then go out.
      seedConversationsCache([conv("conv_abc", "running")]);
      useChatStore.setState({ conversationId: "conv_abc", backgroundTaskCount: 1 });
      handleSessionEvent({
        type: "session_status",
        conversationId: "conv_abc",
        status: "idle",
        backgroundTaskCount: 0,
      });
      expect(readConversationRows().find((c) => c.id === "conv_abc")?.status).toBe("idle");
    });
  });

  describe("session.agent_changed", () => {
    /**
     * Re-route GET /v1/sessions/conv_sw to a post-switch snapshot: bound
     * to the cloned agent and carrying the claude-native wrapper label
     * (the switch route recomputes presentation labels for the target
     * harness). Everything else falls through to the default handler.
     */
    function serveNativeSnapshot(): void {
      fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.toString();
        if (url.split("?")[0] === "/v1/sessions/conv_sw" && (init?.method ?? "GET") === "GET") {
          return mockResponse({
            id: "conv_sw",
            agent_id: "ag_clone",
            agent_name: "Claude Code (switch ag_clone)",
            status: "idle",
            created_at: 0,
            items: [],
            labels: { "omnigent.wrapper": "claude-code-native-ui" },
          });
        }
        return defaultFetchHandler(input, init);
      });
    }

    it("sdk→native switch: refreshed binding stops idle from clearing the optimistic bubble (first-message regression)", async () => {
      // The reported bug: a session bound while on an SDK agent (debby,
      // isNativeTerminalSession=false) is switched in place to a native
      // agent (Claude Code). The store never re-binds (same URL), so
      // without the agent_changed refresh the stale false flag lets the
      // session.status idle handler wipe the first message's optimistic
      // bubble — it only reappears when the slow transcript round-trip
      // finally emits session.input.consumed.
      seedSession("conv_sw", []);
      await useChatStore.getState().switchTo("conv_sw");
      // Pre-switch: default snapshot has no wrapper label → SDK lifecycle.
      expect(useChatStore.getState().isNativeTerminalSession).toBe(false);

      serveNativeSnapshot();
      handleSessionEvent({
        type: "session_agent_changed",
        conversationId: "conv_sw",
        agentId: "ag_clone",
        agentName: "Claude Code (switch ag_clone)",
      });
      // The handler kicks an async snapshot refetch; the flag flipping to
      // true proves the fresh (native-labelled) snapshot was applied.
      await vi.waitFor(() => {
        expect(useChatStore.getState().isNativeTerminalSession).toBe(true);
      });

      // First message after the switch: optimistic bubble + idle churn
      // from the native cold start. With the refreshed flag the native
      // guard holds the bubble for the lagging consumed event; before the
      // fix this was [] — the disappeared-first-message bug.
      useChatStore.setState({
        status: "streaming",
        activeResponse: null,
        pendingUserMessages: [
          { tempId: "pend_first", content: [{ type: "input_text", text: "hi new agent" }] },
        ],
      });
      handleSessionEvent({
        type: "session_status",
        conversationId: "conv_sw",
        status: "idle",
      });
      expect(useChatStore.getState().pendingUserMessages).toEqual([
        { tempId: "pend_first", content: [{ type: "input_text", text: "hi new agent" }] },
      ]);
    });

    it("applies the event's binding immediately and leaves transcript state untouched", async () => {
      const items: ConversationItem[] = [
        userMessage("resp_1", "before switch"),
        assistantMessage("resp_1", "answer"),
      ];
      seedSession("conv_sw", items);
      await useChatStore.getState().switchTo("conv_sw");
      const blocksBefore = useChatStore.getState().blocks;
      expect(blocksBefore.length).toBe(2);
      const pending = [
        { tempId: "pend_keep", content: [{ type: "input_text" as const, text: "queued" }] },
      ];
      useChatStore.setState({ pendingUserMessages: pending });
      const spy = vi.spyOn(client, "invalidateQueries");

      serveNativeSnapshot();
      handleSessionEvent({
        type: "session_agent_changed",
        conversationId: "conv_sw",
        agentId: "ag_clone",
        agentName: "Claude Code (switch ag_clone)",
      });

      // Synchronous patch from the event itself — no fetch needed for the
      // header to show the new agent.
      expect(useChatStore.getState().boundAgentId).toBe("ag_clone");
      expect(useChatStore.getState().boundAgentName).toBe("Claude Code (switch ag_clone)");
      // Observers (other tabs/users) learn about the switch only via this
      // event — the header card and sidebar row must refetch.
      expect(spy).toHaveBeenCalledWith({ queryKey: ["session-agent", "conv_sw"] });
      expect(spy).toHaveBeenCalledWith({ queryKey: ["conversations"] });

      await vi.waitFor(() => {
        expect(useChatStore.getState().isNativeTerminalSession).toBe(true);
      });
      const state = useChatStore.getState();
      // An in-place switch keeps the SAME session/transcript: the refresh
      // must not rebuild or clear blocks (same array reference — nothing
      // was touched) nor drop un-acked optimistic bubbles.
      expect(state.blocks).toBe(blocksBefore);
      expect(state.pendingUserMessages).toEqual(pending);
    });

    it("resets the switched session's terminal cache and refetches it", async () => {
      seedSession("conv_sw", []);
      await useChatStore.getState().switchTo("conv_sw");
      // Stale entry: the old agent's terminal. The switch's runner-side
      // reset closes it, but the cache is SSE-primary with union-on-fetch
      // semantics, so a missed `session.resource.deleted` would pin it
      // forever — the dead "tui · Closed" tab whose attach fails.
      client.setQueryData<TerminalInfo[]>(terminalsQueryKey("conv_sw"), [
        { id: "terminal_tui_main", name: "tui", session: "main", running: true },
      ]);
      const spy = vi.spyOn(client, "invalidateQueries");

      serveNativeSnapshot();
      handleSessionEvent({
        type: "session_agent_changed",
        conversationId: "conv_sw",
        agentId: "ag_clone",
        agentName: "Claude Code (switch ag_clone)",
      });

      // The stale terminal is dropped synchronously…
      expect(client.getQueryData<TerminalInfo[]>(terminalsQueryKey("conv_sw"))).toEqual([]);
      // …and the authoritative list is refetched (the new agent's
      // terminal still lands via its own `created` event regardless).
      expect(spy).toHaveBeenCalledWith({ queryKey: terminalsQueryKey("conv_sw") });
      await tick();
    });

    it("ignores the event when it targets a different conversation", async () => {
      seedSession("conv_active", []);
      seedSession("conv_other", []);
      await useChatStore.getState().switchTo("conv_active");
      const boundBefore = useChatStore.getState().boundAgentId;

      handleSessionEvent({
        type: "session_agent_changed",
        conversationId: "conv_other",
        agentId: "ag_foreign",
        agentName: "Other Agent",
      });
      await tick();

      // A stray event for another session (e.g. a stale pump draining
      // after navigation) must not relabel the active session's binding.
      expect(useChatStore.getState().boundAgentId).toBe(boundBefore);
      expect(useChatStore.getState().boundAgentName).not.toBe("Other Agent");
      expect(useChatStore.getState().isNativeTerminalSession).toBe(false);
    });

    it("marks workspace-environment stale without refetching (os_env boundary recovery)", async () => {
      seedSession("conv_sw", []);
      await useChatStore.getState().switchTo("conv_sw");
      const spy = vi.spyOn(client, "invalidateQueries");

      handleSessionEvent({
        type: "session_agent_changed",
        conversationId: "conv_sw",
        agentId: "ag_clone",
        agentName: "Claude Code (switch ag_clone)",
      });

      // The new agent may have a different (or no) os_env, changing
      // Files-tab availability — but the server's runner reset hasn't run
      // yet when this event fires, so the handler must mark the env query
      // stale WITHOUT an immediate refetch (refetchType "none"): a refetch
      // now would re-serve the OLD agent's still-cached env. The prompt
      // refetch arrives via session.changed_files.invalidated after the
      // reset; this stale-mark is the recovery path for a lost reset.
      expect(spy).toHaveBeenCalledWith({
        queryKey: ["workspace-environment", "conv_sw"],
        refetchType: "none",
      });
      spy.mockRestore();
    });
  });

  describe("session.terminal_pending", () => {
    it("sets terminalPending=true while the runner spins up the terminal", () => {
      useChatStore.setState({ terminalPending: false });
      const event: SessionTerminalPendingEvent = {
        type: "session_terminal_pending",
        conversationId: "conv_abc",
        pending: true,
      };
      handleSessionEvent(event);
      expect(useChatStore.getState().terminalPending).toBe(true);
    });

    it("clears terminalPending=false once the terminal lands or auto-create fails", () => {
      // The runner emits pending=false in a finally, so both the
      // success and the failure path drop the spinner.
      useChatStore.setState({ terminalPending: true });
      handleSessionEvent({
        type: "session_terminal_pending",
        conversationId: "conv_abc",
        pending: false,
      });
      expect(useChatStore.getState().terminalPending).toBe(false);
    });
  });

  describe("session.sandbox_status", () => {
    it("advances the provisioning indicator through launch stages", () => {
      useChatStore.setState({ sandboxStatus: { stage: "provisioning", error: null } });
      handleSessionEvent({
        type: "session_sandbox_status",
        conversationId: "conv_abc",
        stage: "cloning",
        error: null,
      });
      expect(useChatStore.getState().sandboxStatus).toEqual({ stage: "cloning", error: null });
    });

    it("clears the indicator on ready", () => {
      // `ready` means the session now looks like any host-bound
      // session — retaining the status would strand the banner on.
      useChatStore.setState({ sandboxStatus: { stage: "connecting", error: null } });
      handleSessionEvent({
        type: "session_sandbox_status",
        conversationId: "conv_abc",
        stage: "ready",
        error: null,
      });
      expect(useChatStore.getState().sandboxStatus).toBeNull();
    });

    it("retains the failure reason on failed", () => {
      // The failed status stays so the session page can explain why
      // the sandbox never came up (mirrors the server's cache, which
      // retains failures across reloads).
      useChatStore.setState({ sandboxStatus: { stage: "starting", error: null } });
      handleSessionEvent({
        type: "session_sandbox_status",
        conversationId: "conv_abc",
        stage: "failed",
        error: "managed sandbox launch failed: boom",
      });
      expect(useChatStore.getState().sandboxStatus).toEqual({
        stage: "failed",
        error: "managed sandbox launch failed: boom",
      });
    });
  });

  describe("session.mcp_startup", () => {
    it("mirrors an in-flight startup map for the MCP startup band", () => {
      useChatStore.setState({ mcpStartup: null });
      handleSessionEvent({
        type: "session_mcp_startup",
        conversationId: "conv_abc",
        servers: {
          safe: { status: "ready", error: null },
          "storage-console": { status: "starting", error: null },
        },
      });
      expect(useChatStore.getState().mcpStartup).toEqual({
        safe: { status: "ready", error: null },
        "storage-console": { status: "starting", error: null },
      });
    });

    it("clears the map once every server settles ready", () => {
      // An all-ready map means startup completed cleanly — retaining it
      // would strand the startup band on screen.
      useChatStore.setState({
        mcpStartup: { safe: { status: "starting", error: null } },
      });
      handleSessionEvent({
        type: "session_mcp_startup",
        conversationId: "conv_abc",
        servers: { safe: { status: "ready", error: null } },
      });
      expect(useChatStore.getState().mcpStartup).toBeNull();
    });

    it("retains failed and cancelled servers after startup settles", () => {
      // The settled-with-failures map is what lets the page say which
      // servers never came up (mirrors the Codex TUI's startup warnings).
      useChatStore.setState({ mcpStartup: null });
      handleSessionEvent({
        type: "session_mcp_startup",
        conversationId: "conv_abc",
        servers: {
          safe: { status: "failed", error: "handshake failed" },
          "storage-console": { status: "cancelled", error: null },
        },
      });
      expect(useChatStore.getState().mcpStartup).toEqual({
        safe: { status: "failed", error: "handshake failed" },
        "storage-console": { status: "cancelled", error: null },
      });
    });
  });

  describe("session.skills", () => {
    /**
     * Route GET /v1/sessions/{seedId} to a snapshot carrying `skills`;
     * everything else falls back to the default handler. Skills are
     * runner-owned, so the snapshot is the only place the web client can
     * read a fresh, runner-discovered list.
     */
    function seedSnapshotSkills(
      seedId: string,
      skills: { name: string; description: string }[],
    ): void {
      fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.toString();
        if (url.split("?")[0] === `/v1/sessions/${seedId}` && (init?.method ?? "GET") === "GET") {
          return mockResponse({
            id: seedId,
            agent_id: "agent_xyz",
            status: "idle",
            created_at: 0,
            items: [],
            skills,
          });
        }
        return defaultFetchHandler(input, init);
      });
    }

    it("refetches the snapshot and applies the resolved skills to the store", async () => {
      // The bind-time snapshot served [] because skills are fetched off
      // the hot path. When the background fetch lands, session.skills
      // fires; the handler refetches the now-warm snapshot, whose skills
      // must reach the store so the slash-command menu fills.
      useChatStore.setState({ conversationId: "conv_abc", skills: [] });
      seedSnapshotSkills("conv_abc", [{ name: "grill-me", description: "Interview the user" }]);

      handleSessionEvent({
        type: "session_skills",
        conversationId: "conv_abc",
      });
      await tick();

      expect(useChatStore.getState().skills).toEqual([
        { name: "grill-me", description: "Interview the user" },
      ]);
    });

    it("ignores an event for a conversation that is not live", async () => {
      // Gated on liveness, not on being on screen: a nudge for a conversation
      // this tab holds no entry for has nowhere to land, so it must not fetch.
      useChatStore.setState({
        conversationId: "conv_open",
        skills: [{ name: "kept", description: "open session's skill" }],
      });

      handleSessionEvent({
        type: "session_skills",
        conversationId: "conv_not_live",
      });
      await tick();

      // Guard short-circuits: no fetch issued, open session's list intact.
      expect(fetchMock).not.toHaveBeenCalled();
      expect(useChatStore.getState().skills).toEqual([
        { name: "kept", description: "open session's skill" },
      ]);
    });

    it("applies the refetched skills to a conversation backgrounded mid-flight", async () => {
      // `session_skills` is a one-shot nudge with no replay, and a live
      // background conversation is never re-bound on return — so dropping the
      // result because the user switched away left its slash-command menu empty
      // for as long as the entry stayed live. It must land on the conversation
      // it was fetched for, and only there.
      seedSession("conv_bg_skills", []);
      seedSession("conv_foreground", []);
      await useChatStore.getState().switchTo("conv_bg_skills");
      await useChatStore.getState().switchTo("conv_foreground");
      seedSnapshotSkills("conv_bg_skills", [
        { name: "late", description: "resolved in background" },
      ]);

      handleSessionEvent({
        type: "session_skills",
        conversationId: "conv_bg_skills",
      });
      await tick();

      // The backgrounded conversation has its skills...
      expect(conversationRegistry.peek("conv_bg_skills")!.getState().skills).toEqual([
        { name: "late", description: "resolved in background" },
      ]);
      // ...and the visible conversation was not touched.
      expect(useChatStore.getState().skills).toEqual([]);
    });

    it("leaves the existing skills in place when the refetch fails", async () => {
      // The runner can drop again before the snapshot lands; a failed
      // fetch is best-effort and must not wipe a populated list.
      useChatStore.setState({
        conversationId: "conv_abc",
        skills: [{ name: "kept", description: "survives the error" }],
      });
      fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.toString();
        if (url.split("?")[0] === "/v1/sessions/conv_abc" && (init?.method ?? "GET") === "GET") {
          return mockResponse(null, { ok: false, status: 503 });
        }
        return defaultFetchHandler(input, init);
      });

      handleSessionEvent({
        type: "session_skills",
        conversationId: "conv_abc",
      });
      await tick();

      expect(useChatStore.getState().skills).toEqual([
        { name: "kept", description: "survives the error" },
      ]);
    });
  });

  describe("refreshSessionState", () => {
    it("forces a fresh snapshot and applies runner-backed Codex model options", async () => {
      useChatStore.setState({
        conversationId: "conv_codex",
        skills: [],
        codexModelOptions: [],
        terminalPending: false,
      });
      fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.toString();
        if (url.split("?")[0] === "/v1/sessions/conv_codex" && (init?.method ?? "GET") === "GET") {
          return mockResponse({
            id: "conv_codex",
            agent_id: "agent_xyz",
            agent_name: "Codex Agent",
            status: "idle",
            created_at: 0,
            items: [],
            labels: { "omnigent.wrapper": "codex-native-ui" },
            llm_model: "gpt-5.5",
            harness: "codex",
            skills: [{ name: "inspect", description: "Read session state" }],
            model_options: [
              {
                id: "gpt-5.5",
                model: "gpt-5.5",
                displayName: "GPT-5.5",
                defaultReasoningEffort: "medium",
                supportedReasoningEfforts: [
                  { reasoningEffort: "low", description: "Low" },
                  { reasoningEffort: "medium", description: "Medium" },
                  { reasoningEffort: "xhigh", description: "Extra high" },
                ],
                isDefault: true,
              },
            ],
            terminal_pending: true,
          });
        }
        return defaultFetchHandler(input, init);
      });

      await useChatStore.getState().refreshSessionState("conv_codex");

      const sessionFetch = fetchMock.mock.calls.find(([u]) =>
        String(u).startsWith("/v1/sessions/conv_codex?"),
      );
      expect(String(sessionFetch?.[0])).toContain("refresh_state=true");
      expect(useChatStore.getState()).toMatchObject({
        boundAgentName: "Codex Agent",
        isNativeTerminalSession: true,
        llmModel: "gpt-5.5",
        sessionHarness: "codex",
        terminalPending: true,
        skills: [{ name: "inspect", description: "Read session state" }],
        codexModelOptions: [
          {
            id: "gpt-5.5",
            model: "gpt-5.5",
            displayName: "GPT-5.5",
            defaultReasoningEffort: "medium",
            supportedReasoningEfforts: [
              { reasoningEffort: "low", description: "Low" },
              { reasoningEffort: "medium", description: "Medium" },
              { reasoningEffort: "xhigh", description: "Extra high" },
            ],
            isDefault: true,
          },
        ],
      });
    });
  });

  describe("session.model", () => {
    it("lands the harness's verbatim report on llmModel", () => {
      // The harness's model report (the launch's own model, or an in-pane
      // /model switch) arrives as session.model; the reported-model slot
      // follows it VERBATIM, and neither the sticky preference nor the
      // request is touched.
      useChatStore.setState({
        conversationId: "conv_abc",
        selectedModel: "opus",
        sessionModelOverride: "sonnet",
        llmModel: null,
      });
      handleSessionEvent({
        type: "session_model",
        conversationId: "conv_abc",
        model: "system.ai.claude-sonnet-5",
      });
      const state = useChatStore.getState();
      expect(state.llmModel).toBe("system.ai.claude-sonnet-5");
      expect(state.selectedModel).toBe("opus");
      expect(state.sessionModelOverride).toBe("sonnet");
    });

    it("ignores a model event from a different session", () => {
      useChatStore.setState({ conversationId: "conv_open", llmModel: "opus" });
      handleSessionEvent({
        type: "session_model",
        conversationId: "conv_other",
        model: "sonnet",
      });
      expect(useChatStore.getState().llmModel).toBe("opus");
    });

    it("does not resurrect a pending ask the report already settled", async () => {
      // The PATCH is held open while the runner drives and CONFIRMS the
      // switch, so the harness's session.model report usually arrives
      // BEFORE the PATCH resolves. The pending marker is set at ask time
      // and must never be re-set after the response — that stranded the
      // spinner until the hygiene timer.
      seedSession("conv_race", []);
      await useChatStore.getState().switchTo("conv_race");
      let releasePatch: ((r: Response) => void) | null = null;
      fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
        const path = (typeof input === "string" ? input : input.toString()).split("?")[0];
        if (path === "/v1/sessions/conv_race" && init?.method === "PATCH") {
          return new Promise<Response>((resolve) => {
            releasePatch = resolve;
          });
        }
        return defaultFetchHandler(input, init);
      });

      const ask = useChatStore.getState().setModel("sonnet", { expectConfirmation: true });
      await tick();
      // Pending shows from the moment of the ask, not the response.
      expect(useChatStore.getState().pendingModelChange).toBe("sonnet");

      // The harness confirms while the PATCH is still open.
      handleSessionEvent(
        { type: "session_model", conversationId: "conv_race", model: "claude-sonnet-5" },
        "conv_race",
      );
      expect(useChatStore.getState().pendingModelChange).toBeNull();

      releasePatch!(mockResponse({ id: "conv_race", model_override: "sonnet" }));
      await ask;
      expect(useChatStore.getState().pendingModelChange).toBeNull();
      expect(useChatStore.getState().llmModel).toBe("claude-sonnet-5");
    });

    it("settles a pending switch — the report is the ask's outcome", () => {
      // A gear pick marked the ask pending; the harness's own report (in
      // either direction: the pick's confirmation OR an in-pane switch)
      // settles the indicator and flips the reported model.
      useChatStore.setState({
        conversationId: "conv_abc",
        llmModel: "claude-sonnet-5",
        pendingModelChange: "opus",
      });
      handleSessionEvent({
        type: "session_model",
        conversationId: "conv_abc",
        model: "claude-opus-4-10",
      });
      const state = useChatStore.getState();
      expect(state.llmModel).toBe("claude-opus-4-10");
      expect(state.pendingModelChange).toBeNull();
    });

    it("settles a pending switch on the not-applied error", () => {
      // The runner answered non-2xx (swallowed dialog / no bridge): the
      // server publishes the model_change_not_applied error. The pending
      // indicator clears; the reported model stays — the chip never lied.
      useChatStore.setState({
        conversationId: "conv_abc",
        llmModel: "claude-sonnet-5",
        pendingModelChange: "haiku",
      });
      handleSessionEvent({
        type: "error",
        source: "execution",
        toolName: null,
        error: {
          code: "model_change_not_applied",
          message: "The terminal was not switched to haiku.",
        },
      });
      const state = useChatStore.getState();
      expect(state.pendingModelChange).toBeNull();
      expect(state.llmModel).toBe("claude-sonnet-5");
    });

    it("leaves pending state alone for unrelated errors", () => {
      useChatStore.setState({
        conversationId: "conv_abc",
        pendingModelChange: "haiku",
      });
      handleSessionEvent({
        type: "error",
        source: "execution",
        toolName: null,
        error: { code: "tool_failed", message: "boom" },
      });
      expect(useChatStore.getState().pendingModelChange).toBe("haiku");
    });
  });

  describe("session.reasoning_effort", () => {
    it("reflects a TUI-side effort switch in selectedEffort", () => {
      // A thinking-level change inside a native terminal arrives as
      // session.reasoning_effort; the effort picker must follow it.
      useChatStore.setState({ conversationId: "conv_abc", selectedEffort: "high" });
      handleSessionEvent({
        type: "session_reasoning_effort",
        conversationId: "conv_abc",
        reasoningEffort: "medium",
      });
      expect(useChatStore.getState().selectedEffort).toBe("medium");
    });

    it("reflects a terminal effort clear in selectedEffort", () => {
      useChatStore.setState({ conversationId: "conv_abc", selectedEffort: "medium" });
      handleSessionEvent({
        type: "session_reasoning_effort",
        conversationId: "conv_abc",
        reasoningEffort: null,
      });
      expect(useChatStore.getState().selectedEffort).toBeNull();
    });

    it("ignores an effort event from a different session", () => {
      useChatStore.setState({ conversationId: "conv_open", selectedEffort: "high" });
      handleSessionEvent({
        type: "session_reasoning_effort",
        conversationId: "conv_other",
        reasoningEffort: "medium",
      });
      expect(useChatStore.getState().selectedEffort).toBe("high");
    });

    it("records a backgrounded conversation's effort switch on that conversation", () => {
      // The session-scoped value always lands, background included: a live entry
      // is never re-bound on return, so discarding the event left the picker
      // showing the wrong effort until a refresh. The app-global sticky pick is
      // still only adopted from the conversation on screen.
      bindConversationForTest("conv_effort_bg");
      bindConversationForTest("conv_effort_fg");
      useChatStore.setState({ selectedEffort: "high" });

      // Delivered by the backgrounded conversation's OWN stream, which is how
      // this arrives in production.
      handleSessionEvent(
        {
          type: "session_reasoning_effort",
          conversationId: "conv_effort_bg",
          reasoningEffort: "low",
        },
        "conv_effort_bg",
      );

      expect(conversationRegistry.peek("conv_effort_bg")!.getState().sessionReasoningEffort).toBe(
        "low",
      );
      // The visible conversation's sticky pick is untouched.
      expect(useChatStore.getState().selectedEffort).toBe("high");
    });
  });

  describe("session.collaboration_mode", () => {
    it("reflects a collaboration-mode event for the active session", () => {
      useChatStore.setState({ conversationId: "conv_abc", codexPlanMode: false });
      handleSessionEvent({
        type: "session_collaboration_mode",
        conversationId: "conv_abc",
        mode: "plan",
      });
      expect(useChatStore.getState().codexPlanMode).toBe(true);
    });

    it("ignores a collaboration-mode event from a different session", () => {
      useChatStore.setState({ conversationId: "conv_open", codexPlanMode: false });
      handleSessionEvent({
        type: "session_collaboration_mode",
        conversationId: "conv_other",
        mode: "plan",
      });
      expect(useChatStore.getState().codexPlanMode).toBe(false);
    });
  });

  describe("session.input.consumed", () => {
    it("promotes the oldest pending user message into blocks (FIFO, plain append)", () => {
      const existingAssistant: AnyBlock = {
        type: "text_done",
        ctx: {
          agent: null,
          depth: 0,
          turn: 0,
          timestamp: 0,
          responseId: "resp_prev",
          itemId: "msg_asst_prev",
        },
        fullText: "from a prior turn",
        hasCodeBlocks: false,
      };
      useChatStore.setState({
        blocks: [existingAssistant],
        pendingUserMessages: [
          { tempId: "pend_1", content: [{ type: "input_text", text: "first" }] },
          { tempId: "pend_2", content: [{ type: "input_text", text: "second" }] },
        ],
      });

      const event: SessionInputConsumedEvent = {
        type: "session_input_consumed",
        itemId: "msg_persisted_first",
        itemType: "message",
        data: { role: "user", content: [{ type: "input_text", text: "first" }] },
      };
      handleSessionEvent(event);

      const state = useChatStore.getState();
      // Promotion is a plain append at the tail of `blocks` with the
      // server-assigned item id from the event. Order matters:
      // pre-existing blocks first, then the freshly committed user_message.
      expect(state.blocks).toHaveLength(2);
      expect(state.blocks[0]).toBe(existingAssistant);
      const promoted = state.blocks[1] as UserMessageBlock;
      expect(promoted.type).toBe("user_message");
      expect(promoted.ctx.itemId).toBe("msg_persisted_first");
      expect(promoted.content).toEqual([{ type: "input_text", text: "first" }]);
      // stableKey carries the popped optimistic temp id so the rendered
      // bubble keeps its React key (`user:pend_1`) across the
      // optimistic→committed swap — without it the key would change to
      // `user:msg_persisted_first`, remounting the node (the flink).
      expect(promoted.stableKey).toBe("pend_1");
      // Only the oldest pending entry is removed; the rest stays in FIFO order.
      expect(state.pendingUserMessages).toEqual([
        { tempId: "pend_2", content: [{ type: "input_text", text: "second" }] },
      ]);
    });

    it("clears the pending echo without re-appending when the committed item already rendered", () => {
      // Native-terminal race: the forwarder-mirrored user item reached
      // `blocks` (via the stream or a snapshot merge) BEFORE this
      // consumed event. Pre-fix, the handler bailed on the committed-item
      // guard without dropping the optimistic bubble, stranding a
      // duplicate user bubble at the transcript tail forever.
      const committed: AnyBlock = {
        type: "user_message",
        ctx: {
          agent: null,
          depth: 0,
          turn: 0,
          timestamp: 0,
          responseId: "resp_1",
          itemId: "msg_already_committed",
        },
        content: [{ type: "input_text", text: "hello" }],
      } as unknown as AnyBlock;
      useChatStore.setState({
        blocks: [committed],
        pendingUserMessages: [
          { tempId: "pend_1", content: [{ type: "input_text", text: "hello" }], posted: true },
        ],
      });

      handleSessionEvent({
        type: "session_input_consumed",
        itemId: "msg_already_committed",
        itemType: "message",
        data: { role: "user", content: [{ type: "input_text", text: "hello" }] },
      });

      const state = useChatStore.getState();
      // No second copy appended — the committed block stands alone.
      expect(state.blocks).toHaveLength(1);
      expect(state.blocks[0]).toBe(committed);
      // And the optimistic echo is acked away, not stranded.
      expect(state.pendingUserMessages).toEqual([]);
    });

    it("drops the exact named pending entry when the committed item already rendered", () => {
      // Same race, but the server names the drained entry via
      // clearedPendingId — the precise match must win over FIFO.
      const committed: AnyBlock = {
        type: "user_message",
        ctx: {
          agent: null,
          depth: 0,
          turn: 0,
          timestamp: 0,
          responseId: "resp_1",
          itemId: "msg_committed_2",
        },
        content: [{ type: "input_text", text: "second" }],
      } as unknown as AnyBlock;
      useChatStore.setState({
        blocks: [committed],
        pendingUserMessages: [
          { tempId: "pend_a", content: [{ type: "input_text", text: "first" }] },
          { tempId: "pend_b", content: [{ type: "input_text", text: "second" }] },
        ],
      });

      handleSessionEvent({
        type: "session_input_consumed",
        itemId: "msg_committed_2",
        itemType: "message",
        clearedPendingId: "pend_b",
        data: { role: "user", content: [{ type: "input_text", text: "second" }] },
      });

      const state = useChatStore.getState();
      expect(state.blocks).toHaveLength(1);
      expect(state.pendingUserMessages).toEqual([
        { tempId: "pend_a", content: [{ type: "input_text", text: "first" }] },
      ]);
    });

    it("holds the pending head back when the committed item is a mirrored interrupt marker", () => {
      // Claude's own `[Request interrupted by user]` record owns no
      // pending entry and arrives with clearedPendingId unset. A snapshot
      // merge can commit it into `blocks` before its consumed event, so it
      // lands on the committed-item branch. A genuine queued message sits
      // at the pending head — dropping it here (the FIFO fallback without a
      // marker guard) would make the user's in-flight bubble vanish. The
      // marker must not steal that slot, matching the promote path below.
      const committedMarker: AnyBlock = {
        type: "user_message",
        ctx: {
          agent: null,
          depth: 0,
          turn: 0,
          timestamp: 0,
          responseId: "resp_1",
          itemId: "msg_interrupt",
        },
        content: [{ type: "input_text", text: "[Request interrupted by user]" }],
      } as unknown as AnyBlock;
      useChatStore.setState({
        blocks: [committedMarker],
        pendingUserMessages: [
          { tempId: "pend_real", content: [{ type: "input_text", text: "still sending" }] },
        ],
      });

      handleSessionEvent({
        type: "session_input_consumed",
        itemId: "msg_interrupt",
        itemType: "message",
        // No clearedPendingId — the server never drains a pending entry for
        // the CLI's synthetic interrupt record.
        data: {
          role: "user",
          content: [{ type: "input_text", text: "[Request interrupted by user]" }],
        },
      });

      const state = useChatStore.getState();
      // The real message's optimistic bubble survives.
      expect(state.pendingUserMessages).toEqual([
        { tempId: "pend_real", content: [{ type: "input_text", text: "still sending" }] },
      ]);
      // No second copy appended — the committed marker stands alone.
      expect(state.blocks).toHaveLength(1);
      expect(state.blocks[0]).toBe(committedMarker);
    });

    it("threads the event's createdBy onto the promoted committed block", () => {
      // Multi-user attribution: the consumed event carries the human
      // author; the promoted block's ctx.createdBy must reflect it so
      // the bubble shows the author label. Absent => no label.
      useChatStore.setState({
        blocks: [],
        pendingUserMessages: [{ tempId: "pend_1", content: [{ type: "input_text", text: "hi" }] }],
      });

      handleSessionEvent({
        type: "session_input_consumed",
        itemId: "msg_1",
        itemType: "message",
        createdBy: "alice@example.com",
        data: { role: "user", content: [{ type: "input_text", text: "hi" }] },
      });

      const promoted = useChatStore.getState().blocks[0] as UserMessageBlock;
      expect(promoted.ctx.createdBy).toBe("alice@example.com");
      // stableKey still carries the optimistic temp id (no remount on swap).
      expect(promoted.stableKey).toBe("pend_1");
    });

    it("promotes using server file_ids, replacing 'pending:' placeholders", () => {
      // When the user attaches an image, the optimistic pending block carries
      // a "pending:<filename>" file_id as a placeholder. Once the server
      // confirms the upload via session_input_consumed, the committed block
      // must use the real server-assigned file_id so the <img> renders
      // immediately — without requiring a page refresh.
      useChatStore.setState({
        blocks: [],
        pendingUserMessages: [
          {
            tempId: "pend_1",
            content: [
              { type: "input_image", file_id: "pending:photo.png", filename: "photo.png" },
              { type: "input_text", text: "look at this" },
            ],
          },
        ],
      });

      const event: SessionInputConsumedEvent = {
        type: "session_input_consumed",
        itemId: "msg_persisted_1",
        itemType: "message",
        data: {
          role: "user",
          content: [
            { type: "input_image", file_id: "file_abc123", filename: "photo.png" },
            { type: "input_text", text: "look at this" },
          ],
        },
      };
      handleSessionEvent(event);

      const state = useChatStore.getState();
      expect(state.pendingUserMessages).toEqual([]);
      const promoted = state.blocks[0] as UserMessageBlock;
      expect(promoted.type).toBe("user_message");
      expect(promoted.ctx.itemId).toBe("msg_persisted_1");
      // Real file_id from the server — not the "pending:" placeholder.
      expect(promoted.content).toEqual([
        { type: "input_image", file_id: "file_abc123", filename: "photo.png" },
        { type: "input_text", text: "look at this" },
      ]);
    });

    it("renders a cross-client user message when nothing is pending", () => {
      const existingBlocks: AnyBlock[] = [
        {
          type: "text_done",
          ctx: {
            agent: null,
            depth: 0,
            turn: 0,
            timestamp: 0,
            responseId: "resp_prev",
            itemId: "msg_asst",
          },
          fullText: "prior",
          hasCodeBlocks: false,
        },
      ];
      useChatStore.setState({
        blocks: existingBlocks,
        pendingUserMessages: [],
      });

      handleSessionEvent({
        type: "session_input_consumed",
        itemId: "msg_from_other_client",
        itemType: "message",
        data: {
          role: "user",
          content: [{ type: "input_text", text: "from web UI" }],
        },
      });

      const after = useChatStore.getState();
      expect(after.blocks).toHaveLength(2);
      expect(after.blocks[0]).toBe(existingBlocks[0]);
      const promoted = after.blocks[1] as UserMessageBlock;
      expect(promoted.type).toBe("user_message");
      expect(promoted.ctx.itemId).toBe("msg_from_other_client");
      expect(promoted.content).toEqual([{ type: "input_text", text: "from web UI" }]);
      expect(after.pendingUserMessages).toEqual([]);
    });

    it("threads createdBy onto a cross-client user message for live attribution", () => {
      // created_by on the live event sets ctx.createdBy immediately, so a
      // collaborator's message is labeled without waiting for a refresh.
      useChatStore.setState({ blocks: [], pendingUserMessages: [] });

      handleSessionEvent({
        type: "session_input_consumed",
        itemId: "msg_from_collab",
        itemType: "message",
        createdBy: "bob@example.com",
        data: { role: "user", content: [{ type: "input_text", text: "from bob" }] },
      });

      const promoted = useChatStore.getState().blocks[0] as UserMessageBlock;
      expect(promoted.ctx.createdBy).toBe("bob@example.com");
    });

    it("leaves ctx.createdBy unset when the event omits an author", () => {
      // No created_by (agent/system items, single-user "local") must not
      // produce a label; ctx.createdBy stays undefined rather than null.
      useChatStore.setState({ blocks: [], pendingUserMessages: [] });

      handleSessionEvent({
        type: "session_input_consumed",
        itemId: "msg_no_author",
        itemType: "message",
        data: { role: "user", content: [{ type: "input_text", text: "anon" }] },
      });

      const promoted = useChatStore.getState().blocks[0] as UserMessageBlock;
      expect(promoted.ctx.createdBy).toBeUndefined();
    });

    it("dedupes repeated consumed events for the same item id", () => {
      const existing: UserMessageBlock = {
        type: "user_message",
        ctx: {
          agent: null,
          depth: 0,
          turn: 0,
          timestamp: 0,
          responseId: "",
          itemId: "msg_replayed_prompt",
        },
        content: [{ type: "input_text", text: "run tests" }],
      };
      useChatStore.setState({
        blocks: [existing],
        pendingUserMessages: [],
      });

      handleSessionEvent({
        type: "session_input_consumed",
        itemId: "msg_replayed_prompt",
        itemType: "message",
        data: {
          role: "user",
          content: [{ type: "input_text", text: "run tests" }],
        },
      });

      const after = useChatStore.getState();
      // A replayed live event must not append another user bubble.
      // Refresh hydrates from the canonical snapshot and shows one item;
      // this assertion keeps the live view equally idempotent.
      expect(after.blocks).toEqual([existing]);
      expect(after.pendingUserMessages).toEqual([]);
    });

    it("ignores meta input events without consuming pending user messages", () => {
      const existingBlocks: AnyBlock[] = [];
      useChatStore.setState({
        blocks: existingBlocks,
        pendingUserMessages: [
          { tempId: "pend_1", content: [{ type: "input_text", text: "visible pending" }] },
        ],
      });

      handleSessionEvent({
        type: "session_input_consumed",
        itemId: "msg_meta",
        itemType: "message",
        isMeta: true,
        data: {
          role: "user",
          is_meta: true,
          content: [{ type: "input_text", text: "<skill>hidden</skill>" }],
        },
      });

      const after = useChatStore.getState();
      expect(after.blocks).toBe(existingBlocks);
      expect(after.pendingUserMessages).toEqual([
        { tempId: "pend_1", content: [{ type: "input_text", text: "visible pending" }] },
      ]);
    });

    it("is a no-op for non-message item types (e.g. function_call_output from other client)", () => {
      const existingBlocks: AnyBlock[] = [];
      useChatStore.setState({ blocks: existingBlocks, pendingUserMessages: [] });

      handleSessionEvent({
        type: "session_input_consumed",
        itemId: "fnout_xyz",
        itemType: "function_call_output",
        data: {},
      });

      const after = useChatStore.getState();
      expect(after.blocks).toBe(existingBlocks);
    });

    it("drops the entry named by clearedPendingId, not the FIFO head", () => {
      // After a rebind the optimistic bubbles are keyed by the SERVER
      // pending id (from the snapshot / adopted on POST). The server
      // tells us exactly which entry a consumed message drained, so we
      // must drop THAT one — even when it isn't the oldest. Dropping the
      // head here would clear the wrong bubble.
      useChatStore.setState({
        blocks: [],
        pendingUserMessages: [
          { tempId: "pending_old", content: [{ type: "input_text", text: "older" }] },
          { tempId: "pending_new", content: [{ type: "input_text", text: "newer" }] },
        ],
      });

      handleSessionEvent({
        type: "session_input_consumed",
        itemId: "msg_newer",
        itemType: "message",
        clearedPendingId: "pending_new",
        data: { role: "user", content: [{ type: "input_text", text: "newer" }] },
      });

      const state = useChatStore.getState();
      // The id-named entry is promoted; the older one stays pending.
      expect(state.pendingUserMessages).toEqual([
        { tempId: "pending_old", content: [{ type: "input_text", text: "older" }] },
      ]);
      const promoted = state.blocks[0] as UserMessageBlock;
      expect(promoted.ctx.itemId).toBe("msg_newer");
      expect(promoted.content).toEqual([{ type: "input_text", text: "newer" }]);
    });

    it("drops the matched entry by id even when the consumed text was reformatted", () => {
      // Regression: a queued message with a reply-quote is POSTed as a
      // "> blockquote" preamble, but the native transcript round-trips it
      // back as differently-formatted text. The earlier text-match guard
      // failed to recognize them as the same message, so the entry never
      // drained — the message double-rendered (committed + stranded
      // pending) and survived reload. Promotion is now by id (the server
      // names the drained FIFO entry), independent of the text, so the
      // reformatted consumed event still clears the exact bubble.
      useChatStore.setState({
        blocks: [],
        pendingUserMessages: [
          {
            tempId: "pending_quoted",
            content: [{ type: "input_text", text: "> quoted line\n\nIs this the only trigger?" }],
          },
        ],
      });

      handleSessionEvent({
        type: "session_input_consumed",
        itemId: "msg_quoted",
        itemType: "message",
        clearedPendingId: "pending_quoted",
        // Reformatted by the transcript — no blockquote markers, collapsed
        // whitespace. A text comparison against the pending entry fails.
        data: {
          role: "user",
          content: [{ type: "input_text", text: "quoted line Is this the only trigger?" }],
        },
      });

      const state = useChatStore.getState();
      // Exactly one committed bubble, no stranded pending entry.
      expect(state.pendingUserMessages).toEqual([]);
      expect(state.blocks).toHaveLength(1);
      const promoted = state.blocks[0] as UserMessageBlock;
      expect(promoted.ctx.itemId).toBe("msg_quoted");
      // Committed bubble shows the server's (reformatted) content.
      expect(promoted.content).toEqual([
        { type: "input_text", text: "quoted line Is this the only trigger?" },
      ]);
    });

    it("FIFO-promotes the head regardless of text when clearedPendingId is absent", () => {
      // Race / cross-client: the consumed event arrives before the POST
      // response let the sender adopt the server id (so no id match), or
      // it carries no cleared id. The head is promoted by FIFO order
      // WITHOUT comparing text — a text guard would strand a reformatted
      // message as a duplicate (the bug this regression guards). Here the
      // event text differs from the pending entry, yet the head drains.
      useChatStore.setState({
        blocks: [],
        pendingUserMessages: [
          {
            tempId: "pend_local",
            content: [{ type: "input_text", text: "> q\n\nthe original draft" }],
          },
        ],
      });

      handleSessionEvent({
        type: "session_input_consumed",
        itemId: "msg_hi",
        itemType: "message",
        clearedPendingId: null,
        data: { role: "user", content: [{ type: "input_text", text: "the original draft" }] },
      });

      const state = useChatStore.getState();
      // Head drained (not stranded), exactly one committed bubble.
      expect(state.pendingUserMessages).toEqual([]);
      expect(state.blocks).toHaveLength(1);
      expect((state.blocks[0] as UserMessageBlock).ctx.itemId).toBe("msg_hi");
    });
  });

  describe("slash_command (claude-native skill / surfaced command)", () => {
    it("pops the FIFO head of pendingUserMessages so the optimistic bubble clears", () => {
      // Claude-native skips `session.input.consumed` for slash invocations;
      // the slash_command output_item is the only ack, so it must clear
      // the optimistic bubble that `send` parked in `pendingUserMessages`.
      useChatStore.setState({
        blocks: [],
        pendingUserMessages: [
          { tempId: "pend_1", content: [{ type: "input_text", text: "/mlflow-bug" }] },
          { tempId: "pend_2", content: [{ type: "input_text", text: "next" }] },
        ],
      });

      const event: StreamEvent = {
        type: "slash_command",
        kind: "skill",
        name: "mlflow-bug",
        arguments: "",
        output: null,
        agentName: "claude-native-ui",
        itemId: "item_slash_1",
        responseId: "resp_slash_1",
      };
      handleSessionEvent(event);

      expect(useChatStore.getState().pendingUserMessages).toEqual([
        { tempId: "pend_2", content: [{ type: "input_text", text: "next" }] },
      ]);
    });

    it("is a no-op when pendingUserMessages is empty (observing client)", () => {
      useChatStore.setState({ blocks: [], pendingUserMessages: [] });

      const event: StreamEvent = {
        type: "slash_command",
        kind: "command",
        name: "clear",
        arguments: "",
        output: null,
        agentName: "claude-native-ui",
        itemId: "item_slash_2",
        responseId: "resp_slash_2",
      };
      handleSessionEvent(event);

      expect(useChatStore.getState().pendingUserMessages).toEqual([]);
    });
  });

  describe("session.interrupted", () => {
    it("sets activeResponse.state to 'cancelled'", () => {
      useChatStore.setState({
        activeResponse: { responseId: "resp_1", state: "streaming", error: null },
      });

      const event: SessionInterruptedEvent = {
        type: "session_interrupted",
        requestedAt: 1704067200,
      };
      handleSessionEvent(event);

      expect(useChatStore.getState().activeResponse).toEqual({
        responseId: "resp_1",
        state: "cancelled",
        error: null,
        completedAt: expect.any(Number),
      });
    });

    it("is a no-op when there is no active response", () => {
      useChatStore.setState({ activeResponse: null });
      handleSessionEvent({ type: "session_interrupted", requestedAt: 0 });
      expect(useChatStore.getState().activeResponse).toBeNull();
    });
  });

  describe("session.created", () => {
    it("is a no-op (sub-agent rendering is future work — R8)", () => {
      const before = useChatStore.getState();
      const event: SessionCreatedEvent = {
        type: "session_created",
        conversationId: "conv_parent",
        childSessionId: "conv_child",
        agentId: "ag_xyz",
        parentSessionId: "conv_parent",
      };
      handleSessionEvent(event);
      // Same reference — no setState call fired.
      expect(useChatStore.getState()).toBe(before);
    });
  });

  describe("routing by delivering stream", () => {
    // Events are routed by the stream that delivered them, not by their
    // payload: `session.input.consumed`, `session.interrupted` and others carry
    // no conversation id at all, so their contents cannot say where they belong.
    //
    // Each case asserts BOTH halves of that contract — the background
    // conversation receives the event, and the visible one is untouched.
    // Asserting only the second half is what let a bug through: a gate that
    // dropped every background write also left the visible conversation
    // untouched, so those events vanished (a user bubble whose consumed event
    // was dropped never committed, and stayed pinned below the assistant output
    // that arrived after it).
    let bg: ReturnType<typeof bindConversationForTest>;

    beforeEach(() => {
      // Bind conv_bg first, then conv_visible — so conv_bg is live in the
      // registry but backgrounded, exactly as after a switch.
      bg = bindConversationForTest("conv_bg");
      bindConversationForTest("conv_visible");
    });

    it("applies a status patch to the background conversation, not the visible one", () => {
      useChatStore.setState({ sessionStatus: "idle", status: "idle" });
      handleSessionEvent(
        { type: "session_status", conversationId: "conv_bg", status: "running" },
        "conv_bg",
      );
      // conv_bg's own turn lifecycle advances...
      expect(bg.get().sessionStatus).toBe("running");
      // ...while the visible conversation stays idle: a background agent's turn
      // must not light up this conversation's "Working…".
      expect(useChatStore.getState().sessionStatus).toBe("idle");
      expect(useChatStore.getState().status).toBe("idle");
    });

    it("applies a todo list to the background conversation, not the visible one", () => {
      useChatStore.setState({ todos: [] });
      const todos = [
        { content: "bg work", status: "in_progress" as const, activeForm: "Doing bg work" },
      ];
      handleSessionEvent({ type: "session_todos", conversationId: "conv_bg", todos }, "conv_bg");
      expect(bg.get().todos).toEqual(todos);
      expect(useChatStore.getState().todos).toEqual([]);
    });

    it("commits a background conversation's consumed input onto its own transcript", () => {
      // The regression this covers: `session.input.consumed` is the ONLY thing
      // that promotes an optimistic user bubble into committed `blocks`. Drop it
      // for a background conversation and the bubble never commits — it renders
      // as a permanent trailing bubble, below assistant output that arrived
      // after it, which reads as scrambled message ordering on return.
      bindConversationForTest("conv_bg", {
        blocks: [],
        pendingUserMessages: [
          { tempId: "pend_bg", content: [{ type: "input_text", text: "from background" }] },
        ],
      });
      bindConversationForTest("conv_visible");
      useChatStore.setState({ blocks: [], pendingUserMessages: [] });

      handleSessionEvent(
        {
          type: "session_input_consumed",
          itemId: "item_bg",
          itemType: "message",
          data: { role: "user", content: [{ type: "input_text", text: "from background" }] },
        },
        "conv_bg",
      );

      // Promoted on conv_bg: popped off pending, appended to its blocks.
      expect(bg.get().pendingUserMessages).toEqual([]);
      expect(bg.get().blocks.map((b) => b.ctx.itemId)).toEqual(["item_bg"]);
      // The visible conversation never sees it.
      expect(useChatStore.getState().blocks).toEqual([]);
    });

    it("applies a usage update to the background conversation, not the visible one", () => {
      useChatStore.setState({ tokensUsed: 100, sessionCostUsd: 1 });
      handleSessionEvent(
        {
          type: "session_usage",
          conversationId: "conv_bg",
          contextTokens: 99_999,
          totalCostUsd: 42,
        },
        "conv_bg",
      );
      expect(bg.get().tokensUsed).toBe(99_999);
      expect(bg.get().sessionCostUsd).toBe(42);
      expect(useChatStore.getState().tokensUsed).toBe(100);
      expect(useChatStore.getState().sessionCostUsd).toBe(1);
    });

    it("cancels the background conversation's own turn, never the visible one", () => {
      // `session.interrupted` also carries no conversation id.
      bindConversationForTest("conv_bg", {
        activeResponse: { responseId: "resp_bg", state: "streaming", error: null },
        interruptedResponseIds: [],
      });
      bindConversationForTest("conv_visible");
      useChatStore.setState({
        activeResponse: { responseId: "resp_visible", state: "streaming", error: null },
        interruptedResponseIds: [],
      });

      handleSessionEvent(
        { type: "session_interrupted", requestedAt: 0, responseId: "resp_bg" },
        "conv_bg",
      );

      // `toMatchObject`: main's turn lifecycle also stamps `completedAt`.
      expect(bg.get().activeResponse).toMatchObject({
        responseId: "resp_bg",
        state: "cancelled",
        error: null,
      });
      expect(bg.get().interruptedResponseIds).toEqual(["resp_bg"]);
      // The visible turn keeps streaming.
      expect(useChatStore.getState().activeResponse).toEqual({
        responseId: "resp_visible",
        state: "streaming",
        error: null,
      });
      expect(useChatStore.getState().interruptedResponseIds).toEqual([]);
    });

    it("still applies events delivered by the visible conversation's own stream", () => {
      // Routing must not make the normal path a no-op — a gate that dropped
      // everything would satisfy the "visible untouched" half of every case
      // above. This is the positive control for the foreground path.
      handleSessionEvent(
        { type: "session_status", conversationId: "conv_visible", status: "running" },
        "conv_visible",
      );
      expect(useChatStore.getState().sessionStatus).toBe("running");
    });

    it("drops an event whose conversation has been evicted", () => {
      // A late event for a conversation the registry has released must not
      // resurrect its state, and must not fall through onto the visible one.
      releaseConversation("conv_bg");
      useChatStore.setState({ sessionStatus: "idle" });
      handleSessionEvent(
        { type: "session_status", conversationId: "conv_bg", status: "running" },
        "conv_bg",
      );
      expect(useChatStore.getState().sessionStatus).toBe("idle");
    });
  });
});

describe("chatStore — handleSessionEvent (resource events)", () => {
  function makeTerminalResource(
    id: string,
    overrides?: { name?: string; sessionKey?: string; running?: boolean },
  ): Record<string, unknown> {
    return {
      id,
      object: "session.resource",
      type: "terminal",
      session_id: "conv_abc",
      name: `${overrides?.name ?? "bash"}:${overrides?.sessionKey ?? "s1"}`,
      environment: `env_${id}`,
      metadata: {
        terminal_name: overrides?.name ?? "bash",
        session_key: overrides?.sessionKey ?? "s1",
        running: overrides?.running ?? true,
      },
    };
  }

  describe("session.resource.created (terminal)", () => {
    it("appends a new terminal to the cached list", () => {
      client.setQueryData<TerminalInfo[]>(terminalsQueryKey("conv_abc"), []);
      const event: SessionResourceCreatedEvent = {
        type: "session_resource_created",
        resource: makeTerminalResource("terminal_bash_s1") as never,
      };

      handleSessionEvent(event);

      const cached = client.getQueryData<TerminalInfo[]>(terminalsQueryKey("conv_abc"));
      expect(cached).toEqual([
        { id: "terminal_bash_s1", name: "bash", session: "s1", running: true },
      ]);
    });

    it("is idempotent for duplicate resource ids", () => {
      client.setQueryData<TerminalInfo[]>(terminalsQueryKey("conv_abc"), [
        { id: "terminal_bash_s1", name: "bash", session: "s1", running: true },
      ]);
      handleSessionEvent({
        type: "session_resource_created",
        resource: makeTerminalResource("terminal_bash_s1") as never,
      });
      const cached = client.getQueryData<TerminalInfo[]>(terminalsQueryKey("conv_abc"));
      expect(cached).toHaveLength(1);
    });

    it("initializes a cold cache so the count updates without a mounted hook", () => {
      // Cold cache (panel closed / hook not mounted yet). Snapshot-on-
      // connect provides the baseline over the stream, so we seed the
      // cache here rather than waiting for the response-end refetch —
      // this is what makes the terminal count update live.
      handleSessionEvent({
        type: "session_resource_created",
        resource: makeTerminalResource("terminal_bash_s1") as never,
      });
      const cached = client.getQueryData<TerminalInfo[]>(terminalsQueryKey("conv_abc"));
      expect(cached).toEqual([
        { id: "terminal_bash_s1", name: "bash", session: "s1", running: true },
      ]);
    });

    it("ignores non-terminal resource creations", () => {
      client.setQueryData<TerminalInfo[]>(terminalsQueryKey("conv_abc"), []);
      handleSessionEvent({
        type: "session_resource_created",
        resource: {
          id: "file_abc",
          type: "file",
          name: "report.pdf",
          metadata: {},
        } as never,
      });
      const cached = client.getQueryData<TerminalInfo[]>(terminalsQueryKey("conv_abc"));
      expect(cached).toEqual([]);
    });
  });

  describe("session.resource.deleted (terminal)", () => {
    it("removes the matching terminal from the cached list", () => {
      client.setQueryData<TerminalInfo[]>(terminalsQueryKey("conv_abc"), [
        { id: "terminal_bash_s1", name: "bash", session: "s1", running: true },
        { id: "terminal_python_s2", name: "python", session: "s2", running: true },
      ]);
      const event: SessionResourceDeletedEvent = {
        type: "session_resource_deleted",
        resourceId: "terminal_bash_s1",
        resourceType: "terminal",
        sessionId: "conv_abc",
      };

      handleSessionEvent(event);

      const cached = client.getQueryData<TerminalInfo[]>(terminalsQueryKey("conv_abc"));
      expect(cached).toEqual([
        { id: "terminal_python_s2", name: "python", session: "s2", running: true },
      ]);
    });

    it("is a no-op for an unknown resource id", () => {
      const initial: TerminalInfo[] = [
        { id: "terminal_bash_s1", name: "bash", session: "s1", running: true },
      ];
      client.setQueryData<TerminalInfo[]>(terminalsQueryKey("conv_abc"), initial);
      handleSessionEvent({
        type: "session_resource_deleted",
        resourceId: "terminal_nope",
        resourceType: "terminal",
        sessionId: "conv_abc",
      });
      expect(client.getQueryData<TerminalInfo[]>(terminalsQueryKey("conv_abc"))).toEqual(initial);
    });

    it("ignores non-terminal resource deletions", () => {
      const initial: TerminalInfo[] = [
        { id: "terminal_bash_s1", name: "bash", session: "s1", running: true },
      ];
      client.setQueryData<TerminalInfo[]>(terminalsQueryKey("conv_abc"), initial);
      handleSessionEvent({
        type: "session_resource_deleted",
        resourceId: "file_abc",
        resourceType: "file",
        sessionId: "conv_abc",
      });
      expect(client.getQueryData<TerminalInfo[]>(terminalsQueryKey("conv_abc"))).toEqual(initial);
    });
  });

  describe("session.child_session.updated", () => {
    const child = (overrides: Partial<ChildSessionInfo> = {}): Record<string, unknown> => ({
      id: "conv_child1",
      title: "researcher:auth",
      task_summary: null,
      tool: "researcher",
      session_name: "auth",
      current_task_status: "in_progress",
      busy: true,
      last_task_error: null,
      last_message_preview: "looking…",
      ...overrides,
    });

    it("upserts a new child into the cached list", () => {
      client.setQueryData<ChildSessionInfo[]>(childSessionsQueryKey("conv_parent"), []);
      handleSessionEvent({
        type: "session_child_session_updated",
        conversationId: "conv_parent",
        childSessionId: "conv_child1",
        child: child(),
      });
      const cached = client.getQueryData<ChildSessionInfo[]>(childSessionsQueryKey("conv_parent"));
      expect(cached).toEqual([
        {
          id: "conv_child1",
          title: "researcher:auth",
          task_summary: null,
          tool: "researcher",
          session_name: "auth",
          labels: {},
          current_task_status: "in_progress",
          last_task_error: null,
          busy: true,
          last_message_preview: "looking…",
          // Insert path defaults the count to 0 when the delta omits it.
          pending_elicitations_count: 0,
        },
      ]);
    });

    it("merges an existing child in place on a status change", () => {
      client.setQueryData<ChildSessionInfo[]>(childSessionsQueryKey("conv_parent"), [
        {
          id: "conv_child1",
          title: "researcher:auth",
          task_summary: null,
          tool: "researcher",
          session_name: "auth",
          current_task_status: "in_progress",
          busy: true,
          last_message_preview: null,
          pending_elicitations_count: 0,
        },
      ]);
      handleSessionEvent({
        type: "session_child_session_updated",
        conversationId: "conv_parent",
        childSessionId: "conv_child1",
        child: child({ current_task_status: "completed", busy: false }),
      });
      const cached = client.getQueryData<ChildSessionInfo[]>(childSessionsQueryKey("conv_parent"));
      // Still one row (merged, not appended); busy flipped to false.
      expect(cached).toHaveLength(1);
      expect(cached?.[0].busy).toBe(false);
      expect(cached?.[0].current_task_status).toBe("completed");
    });

    it("merges a PARTIAL status delta without clobbering the preview", () => {
      // Runner status deltas omit last_message_preview; the merge must
      // keep the snapshot's preview rather than nulling it.
      client.setQueryData<ChildSessionInfo[]>(childSessionsQueryKey("conv_parent"), [
        {
          id: "conv_child1",
          title: "researcher:auth",
          task_summary: null,
          tool: "researcher",
          session_name: "auth",
          current_task_status: "in_progress",
          busy: true,
          last_message_preview: "digging through auth.py",
          pending_elicitations_count: 0,
        },
      ]);
      handleSessionEvent({
        type: "session_child_session_updated",
        conversationId: "conv_parent",
        childSessionId: "conv_child1",
        // Partial: only busy + status (what a runner status delta carries).
        child: { id: "conv_child1", busy: false, current_task_status: "completed" },
      });
      const row = client.getQueryData<ChildSessionInfo[]>(
        childSessionsQueryKey("conv_parent"),
      )?.[0];
      expect(row?.busy).toBe(false); // updated
      expect(row?.last_message_preview).toBe("digging through auth.py"); // preserved
      expect(row?.title).toBe("researcher:auth"); // preserved
    });

    it("merges a PARTIAL preview delta without clobbering busy/status", () => {
      // Runner preview deltas carry only last_message_preview; busy and
      // status from the prior status delta must survive.
      client.setQueryData<ChildSessionInfo[]>(childSessionsQueryKey("conv_parent"), [
        {
          id: "conv_child1",
          title: "researcher:auth",
          task_summary: null,
          tool: "researcher",
          session_name: "auth",
          current_task_status: "in_progress",
          busy: true,
          last_message_preview: null,
          pending_elicitations_count: 0,
        },
      ]);
      handleSessionEvent({
        type: "session_child_session_updated",
        conversationId: "conv_parent",
        childSessionId: "conv_child1",
        child: { id: "conv_child1", last_message_preview: "found the bug" },
      });
      const row = client.getQueryData<ChildSessionInfo[]>(
        childSessionsQueryKey("conv_parent"),
      )?.[0];
      expect(row?.last_message_preview).toBe("found the bug"); // updated
      expect(row?.busy).toBe(true); // preserved
      expect(row?.current_task_status).toBe("in_progress"); // preserved
    });

    it("merges a failed status delta with a durable error", () => {
      client.setQueryData<ChildSessionInfo[]>(childSessionsQueryKey("conv_parent"), [
        {
          id: "conv_child1",
          title: "researcher:auth",
          task_summary: null,
          tool: "researcher",
          session_name: "auth",
          current_task_status: "in_progress",
          busy: true,
          last_message_preview: "booting worker",
          pending_elicitations_count: 0,
        },
      ]);
      handleSessionEvent({
        type: "session_child_session_updated",
        conversationId: "conv_parent",
        childSessionId: "conv_child1",
        child: {
          id: "conv_child1",
          busy: false,
          current_task_status: "failed",
          last_task_error: {
            code: "required_terminal_exited",
            message: "Required terminal exited unexpectedly",
          },
        },
      });
      const row = client.getQueryData<ChildSessionInfo[]>(
        childSessionsQueryKey("conv_parent"),
      )?.[0];
      expect(row?.busy).toBe(false);
      expect(row?.current_task_status).toBe("failed");
      expect(row?.last_task_error).toEqual({
        code: "required_terminal_exited",
        message: "Required terminal exited unexpectedly",
      });
    });

    it("clears a stale child error when the runner sends an active status", () => {
      client.setQueryData<ChildSessionInfo[]>(childSessionsQueryKey("conv_parent"), [
        {
          id: "conv_child1",
          title: "researcher:auth",
          task_summary: null,
          tool: "researcher",
          session_name: "auth",
          current_task_status: "failed",
          busy: false,
          last_task_error: {
            code: "required_terminal_exited",
            message: "Required terminal exited unexpectedly",
          },
          last_message_preview: "boot failed",
          pending_elicitations_count: 0,
        },
      ]);
      handleSessionEvent({
        type: "session_child_session_updated",
        conversationId: "conv_parent",
        childSessionId: "conv_child1",
        child: {
          id: "conv_child1",
          busy: true,
          current_task_status: "in_progress",
          last_task_error: null,
        },
      });
      const row = client.getQueryData<ChildSessionInfo[]>(
        childSessionsQueryKey("conv_parent"),
      )?.[0];
      expect(row?.busy).toBe(true);
      expect(row?.current_task_status).toBe("in_progress");
      expect(row?.last_task_error).toBeNull();
    });

    it("initializes a cold child-sessions cache from a full delta", () => {
      // Snapshot-on-connect / spawn deltas carry full rows, so a cold
      // cache is seeded (lets child status update without a mounted hook).
      handleSessionEvent({
        type: "session_child_session_updated",
        conversationId: "conv_parent",
        childSessionId: "conv_child1",
        child: child(),
      });
      const cached = client.getQueryData<ChildSessionInfo[]>(childSessionsQueryKey("conv_parent"));
      expect(cached).toHaveLength(1);
      expect(cached?.[0].id).toBe("conv_child1");
      expect(cached?.[0].busy).toBe(true);
    });

    it("refetches the parent's child list when a child goes idle without a preview", () => {
      // claude-native turn-complete deltas carry busy=false but no preview
      // (the reply lives in the tmux pane). Refetch so the server-computed
      // preview lands instead of staying stale.
      client.setQueryData<ChildSessionInfo[]>(childSessionsQueryKey("conv_parent"), []);
      const spy = vi.spyOn(client, "invalidateQueries");
      handleSessionEvent({
        type: "session_child_session_updated",
        conversationId: "conv_parent",
        childSessionId: "conv_child1",
        child: { id: "conv_child1", busy: false },
      });
      expect(spy).toHaveBeenCalledWith({ queryKey: childSessionsQueryKey("conv_parent") });
    });

    it("does NOT refetch when the idle delta already carries a preview", () => {
      // In-process harnesses include the preview in the delta — the in-place
      // patch suffices, so no refetch is triggered.
      client.setQueryData<ChildSessionInfo[]>(childSessionsQueryKey("conv_parent"), []);
      const spy = vi.spyOn(client, "invalidateQueries");
      handleSessionEvent({
        type: "session_child_session_updated",
        conversationId: "conv_parent",
        childSessionId: "conv_child1",
        child: { id: "conv_child1", busy: false, last_message_preview: "done." },
      });
      expect(spy).not.toHaveBeenCalled();
    });
  });

  describe("session.changed_files.invalidated", () => {
    it("coalesces and invalidates filesystem query caches for the session", async () => {
      vi.useFakeTimers();
      const spy = vi.spyOn(client, "invalidateQueries");
      handleSessionEvent({
        type: "session_changed_files_invalidated",
        sessionId: "conv_abc",
        environmentId: "default",
      });
      handleSessionEvent({
        type: "session_changed_files_invalidated",
        sessionId: "conv_abc",
        environmentId: "default",
      });
      expect(spy).not.toHaveBeenCalled();

      await vi.advanceTimersByTimeAsync(750);

      expect(spy).toHaveBeenCalledWith({
        queryKey: ["workspace-changed-files", "conv_abc"],
      });
      expect(spy).toHaveBeenCalledWith({
        queryKey: ["workspace-all-files", "conv_abc"],
      });
      expect(spy).toHaveBeenCalledWith({
        queryKey: ["workspace-dir", "conv_abc"],
        refetchType: "none",
      });
      expect(spy).toHaveBeenCalledWith({
        queryKey: ["workspace-dir-listing", "conv_abc"],
        refetchType: "none",
      });
      // Environment availability refetches too (default refetchType, so the
      // active AppShell query re-runs): the post-switch runner reset
      // publishes this event after closing the old agent's env, and this
      // invalidation is what flips the Files tab across an os_env-boundary
      // agent switch. Missing → the tab stays stale for the 60 s staleTime.
      expect(spy).toHaveBeenCalledWith({
        queryKey: ["workspace-environment", "conv_abc"],
      });
      // 5 = the four filesystem-view keys + workspace-environment, all from
      // ONE debounced flush (the two events above coalesced). 10 would mean
      // the debounce broke and each event flushed separately.
      expect(spy).toHaveBeenCalledTimes(5);
      spy.mockRestore();
    });
  });

  describe("session.terminal.activity", () => {
    it("records an activity pulse so the badge can light without an attach", () => {
      const before = useTerminalActivityStore.getState().lastActive["terminal_zsh_s1"];
      const t0 = Date.now();
      handleSessionEvent({
        type: "session_terminal_activity",
        sessionId: "conv_abc",
        terminalId: "terminal_zsh_s1",
      });
      const after = useTerminalActivityStore.getState().lastActive["terminal_zsh_s1"];
      // A fresh, CURRENT timestamp (>= the moment before dispatch) proves
      // the pulse landed now — not a stale value left from a prior test.
      // useTerminalStatuses reads this against ACTIVE_OUTPUT_WINDOW_MS
      // (1500ms) to decide "active", so an old timestamp wouldn't light.
      expect(typeof after).toBe("number");
      expect(after).not.toBe(before);
      expect(after as number).toBeGreaterThanOrEqual(t0);
    });
  });
});

function elicitationBlock(id: string): ElicitationBlock {
  return {
    type: "elicitation",
    ctx: {
      agent: null,
      depth: 0,
      turn: 0,
      timestamp: 0,
      responseId: "resp_1",
      itemId: null,
    },
    elicitationId: id,
    message: "Approve?",
    phase: "tool_call",
    policyName: "test_policy",
    contentPreview: "",
    requestedSchema: {},
    status: "pending",
    response: null,
  };
}

describe("chatStore — submitApproval", () => {
  it("posts the verdict to the elicitation resolve URL and optimistically marks responded", async () => {
    useChatStore.setState({
      conversationId: "conv_abc",
      blocks: [elicitationBlock("elic_xyz")],
    });

    await useChatStore.getState().submitApproval("elic_xyz", "accept");

    // URL-based elicitation: verdict goes to the dedicated resolve
    // URL (elicitation id in the path), with the bare MCP body.
    const events = fetchMock.mock.calls.filter(([u]) =>
      String(u).endsWith("/v1/sessions/conv_abc/elicitations/elic_xyz/resolve"),
    );
    expect(events).toHaveLength(1);
    const body = JSON.parse((events[0]![1] as RequestInit).body as string);
    expect(body).toEqual({ action: "accept" });

    const block = useChatStore.getState().blocks[0];
    if (block?.type === "elicitation") {
      expect(block.status).toBe("responded");
      expect(block.response).toEqual({ action: "accept" });
    }
  });

  it("preserves Codex execpolicy amendment content when submitting approval", async () => {
    useChatStore.setState({
      conversationId: "conv_abc",
      blocks: [elicitationBlock("elic_cmd")],
    });

    const amendment = [".venv/bin/python", "-m", "pytest"];
    await useChatStore.getState().submitApproval("elic_cmd", "accept", {
      execpolicy_amendment: amendment,
    });

    const events = fetchMock.mock.calls.filter(([u]) =>
      String(u).endsWith("/v1/sessions/conv_abc/elicitations/elic_cmd/resolve"),
    );
    expect(events).toHaveLength(1);
    const body = JSON.parse((events[0]![1] as RequestInit).body as string);
    expect(body).toEqual({
      action: "accept",
      content: { execpolicy_amendment: amendment },
    });

    const block = useChatStore.getState().blocks[0];
    expect(block?.type).toBe("elicitation");
    if (!block || block.type !== "elicitation") {
      throw new Error("expected submitApproval to preserve the elicitation block");
    }
    expect(block.status).toBe("responded");
    expect(block.response).toEqual({
      action: "accept",
      content: { execpolicy_amendment: amendment },
    });
  });

  it("posts mirrored child elicitation verdicts to the child session resolve URL", async () => {
    useChatStore.setState({
      conversationId: "conv_parent",
      blocks: [{ ...elicitationBlock("elic_child"), targetSessionId: "conv_child" }],
    });

    await useChatStore.getState().submitApproval("elic_child", "accept");

    const childCalls = fetchMock.mock.calls.filter(([u]) =>
      String(u).endsWith("/v1/sessions/conv_child/elicitations/elic_child/resolve"),
    );
    const parentCalls = fetchMock.mock.calls.filter(([u]) =>
      String(u).endsWith("/v1/sessions/conv_parent/elicitations/elic_child/resolve"),
    );
    expect(childCalls).toHaveLength(1);
    expect(parentCalls).toHaveLength(0);
    const body = JSON.parse((childCalls[0]![1] as RequestInit).body as string);
    expect(body).toEqual({ action: "accept" });
  });

  it("rolls back to 'pending' when the network call fails", async () => {
    useChatStore.setState({
      conversationId: "conv_abc",
      blocks: [elicitationBlock("elic_xyz")],
    });
    fetchMock.mockImplementationOnce(() => mockResponse({}, { ok: false, status: 500 }));

    await useChatStore.getState().submitApproval("elic_xyz", "accept");

    const block = useChatStore.getState().blocks[0];
    if (block?.type === "elicitation") {
      expect(block.status).toBe("pending");
      expect(block.response).toBeNull();
    }
  });

  it("rolls back on the answering conversation when the failure outlives a switch", async () => {
    // The approve POST can outlive a switch away. Resolving the rollback target
    // late reopened the card on whatever conversation was then visible while
    // leaving the answering one wrongly marked responded — so the user's failed
    // approval silently disappeared and a stranger's card sprouted buttons.
    seedSession("conv_approve", []);
    seedSession("conv_after", []);
    let failResolve: (() => void) | null = null;
    fetchMock.mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith("/v1/sessions/conv_approve/elicitations/elic_late/resolve")) {
        return new Promise<Response>((resolve) => {
          failResolve = () => resolve(mockResponse({}, { ok: false, status: 500 }));
        });
      }
      return defaultFetchHandler(input, init);
    });

    await useChatStore.getState().switchTo("conv_approve");
    conversationRegistry.peek("conv_approve")!.setState({
      blocks: [elicitationBlock("elic_late")],
    });
    const approving = useChatStore.getState().submitApproval("elic_late", "accept");
    await tick();

    await useChatStore.getState().switchTo("conv_after");
    failResolve!();
    await approving;

    // The card is answerable again on the conversation that owns it.
    const card = conversationRegistry.peek("conv_approve")!.getState().blocks[0];
    expect(card?.type).toBe("elicitation");
    if (card?.type !== "elicitation") throw new Error("expected an elicitation block");
    expect(card.status).toBe("pending");
    expect(card.response).toBeNull();
    // The visible conversation gained nothing.
    expect(useChatStore.getState().blocks).toEqual([]);
  });

  it("only updates the matching elicitation by id (other pending blocks untouched)", async () => {
    useChatStore.setState({
      conversationId: "conv_abc",
      blocks: [elicitationBlock("elic_a"), elicitationBlock("elic_b")],
    });

    await useChatStore.getState().submitApproval("elic_b", "decline");

    const blocks = useChatStore.getState().blocks;
    if (blocks[0]?.type === "elicitation") expect(blocks[0].status).toBe("pending");
    if (blocks[1]?.type === "elicitation") {
      expect(blocks[1].status).toBe("responded");
      expect(blocks[1].response).toEqual({ action: "decline" });
    }
  });

  it("no-op when no session is bound", async () => {
    useChatStore.setState({
      conversationId: null,
      blocks: [elicitationBlock("elic_xyz")],
    });

    await useChatStore.getState().submitApproval("elic_xyz", "accept");

    expect(fetchMock).not.toHaveBeenCalled();
    const block = useChatStore.getState().blocks[0];
    if (block?.type === "elicitation") expect(block.status).toBe("pending");
  });
});

describe("chatStore — tool_result does not resolve elicitations", () => {
  function toolResultEvent(callId: string): StreamEvent {
    return {
      type: "tool_result",
      callId,
      output: "ok",
      itemId: `fco_${callId}`,
      responseId: "resp_1",
    };
  }

  function toolCallEvent(callId: string): StreamEvent {
    return {
      type: "tool_call",
      name: "AskUserQuestion",
      arguments: { questions: [] },
      callId,
      status: "completed",
      agentName: "claude-native-ui",
      itemId: `fc_${callId}`,
      responseId: "resp_1",
    };
  }

  it("leaves pending cards untouched on tool_result", () => {
    // A tool result has no elicitation id, so it cannot prove which
    // prompt was answered. Resolving by position regresses Codex
    // sessions with multiple simultaneous prompts: approving one
    // prompt makes its tool result arrive while the other prompt is
    // still genuinely pending.
    useChatStore.setState({
      blocks: [elicitationBlock("elic_older"), elicitationBlock("elic_newer")],
    });

    handleSessionEvent(toolResultEvent("call_xyz"));

    const blocks = useChatStore.getState().blocks;
    const older = blocks.find((b) => b.type === "elicitation" && b.elicitationId === "elic_older");
    const newer = blocks.find((b) => b.type === "elicitation" && b.elicitationId === "elic_newer");
    if (older?.type !== "elicitation" || newer?.type !== "elicitation") {
      throw new Error("expected both elicitation blocks to be present");
    }
    expect(older.status).toBe("pending");
    expect(older.response).toBeNull();
    expect(newer.status).toBe("pending");
    expect(newer.response).toBeNull();
  });

  it("does not mark another pending card resolved after one approval's tool_result", async () => {
    // Regression for Codex app-server elicitations: request A and
    // request B can be visible together. After approving A, the
    // result for A must not flip B to "Resolved elsewhere"; B is
    // still waiting for its own user verdict.
    useChatStore.setState({
      conversationId: "conv_abc",
      blocks: [elicitationBlock("elic_a"), elicitationBlock("elic_b")],
    });

    await useChatStore.getState().submitApproval("elic_a", "accept");
    handleSessionEvent(toolResultEvent("call_for_a"));

    const blocks = useChatStore.getState().blocks;
    const approved = blocks.find((b) => b.type === "elicitation" && b.elicitationId === "elic_a");
    const pending = blocks.find((b) => b.type === "elicitation" && b.elicitationId === "elic_b");
    if (approved?.type !== "elicitation" || pending?.type !== "elicitation") {
      throw new Error("expected both elicitation blocks to be present");
    }
    expect(approved.status).toBe("responded");
    expect(approved.response).toEqual({ action: "accept" });
    expect(pending.status).toBe("pending");
    expect(pending.response).toBeNull();
  });

  it("does NOT clear on tool_call (function_call)", () => {
    // Claude sometimes emits the function_call item BEFORE the
    // user finishes interacting with the tool's TUI prompt
    // (notably AskUserQuestion). Clearing on function_call would
    // prematurely close the card while the picker is still open,
    // and it also carries no elicitation id.
    useChatStore.setState({ blocks: [elicitationBlock("elic_live")] });

    handleSessionEvent(toolCallEvent("call_pending"));

    const block = useChatStore.getState().blocks[0];
    if (block?.type !== "elicitation") {
      throw new Error("expected an elicitation block");
    }
    expect(block.status).toBe("pending");
    expect(block.response).toBeNull();
  });

  it("leaves already-responded cards alone", () => {
    // The user already approved this elicitation via the UI; its
    // response was recorded. A later tool_result must not
    // overwrite the recorded verdict with an auto_resolved
    // synthetic, or the approval history becomes misleading.
    const base = elicitationBlock("elic_already_done");
    if (base.type !== "elicitation") throw new Error("expected elicitation");
    const answered: AnyBlock = {
      ...base,
      status: "responded",
      response: { action: "accept" },
    };
    useChatStore.setState({ blocks: [answered] });

    handleSessionEvent(toolResultEvent("any_call_id"));

    const blocks = useChatStore.getState().blocks;
    const block = blocks[0];
    if (block?.type !== "elicitation") {
      throw new Error("expected an elicitation block");
    }
    expect(block.status).toBe("responded");
    expect(block.response).toEqual({ action: "accept" });
  });

  it("no-op when there are no elicitation blocks to clear", () => {
    // Streams without any approval surface must not crash on
    // tool_result — most tool results in the wild don't have an
    // approval surface behind them.
    useChatStore.setState({ blocks: [] });
    handleSessionEvent(toolResultEvent("call_any"));
    expect(useChatStore.getState().blocks).toEqual([]);
  });
});

describe("chatStore — elicitation_resolved", () => {
  // These cases call `handleSessionEvent` directly, with no stream to name the
  // delivering conversation. Bind one so the write has an entry to land on,
  // exactly as it would in production (`elicitation_resolved` carries no
  // conversation id, so it is routed by its delivering stream).
  beforeEach(() => {
    bindConversationForTest("conv_elic");
  });

  function elicitationResolvedEvent(id: string): StreamEvent {
    return { type: "elicitation_resolved", elicitationId: id };
  }

  it("flips the matching card to auto_resolved by elicitation_id", () => {
    // The server emits this event when an approval is cleared via
    // any path (second tab, REPL TUI, PermissionRequest hook's
    // tool-result auto-resolve). Match by id, not first-pending —
    // that's the whole reason for the explicit signal. If the
    // handler reverts to a heuristic scan, this regresses to the
    // wrong-card behaviour the new event was added to fix.
    useChatStore.setState({
      blocks: [elicitationBlock("elic_older"), elicitationBlock("elic_target")],
    });

    handleSessionEvent(elicitationResolvedEvent("elic_target"));

    const blocks = useChatStore.getState().blocks;
    const older = blocks.find((b) => b.type === "elicitation" && b.elicitationId === "elic_older");
    const target = blocks.find(
      (b) => b.type === "elicitation" && b.elicitationId === "elic_target",
    );
    if (older?.type !== "elicitation" || target?.type !== "elicitation") {
      throw new Error("expected both elicitation blocks to be present");
    }
    // Older stays pending — only the matched id flips.
    expect(older.status).toBe("pending");
    expect(older.response).toBeNull();
    expect(target.status).toBe("responded");
    expect(target.response).toEqual({ action: "auto_resolved" });
  });

  it("leaves already-responded cards alone", () => {
    // A duplicate elicitation_resolved (e.g. the server's
    // approval-dispatch published one, and the hook's finally
    // also fired one for the same id) must not overwrite the
    // recorded verdict. Otherwise the user's "Approved" /
    // "Selected: X" pill would silently flip to "Resolved
    // elsewhere" — losing the action and selections.
    const base = elicitationBlock("elic_done");
    if (base.type !== "elicitation") throw new Error("expected elicitation");
    const answered: AnyBlock = {
      ...base,
      status: "responded",
      response: { action: "accept", content: { answer: "yes" } },
    };
    useChatStore.setState({ blocks: [answered] });

    handleSessionEvent(elicitationResolvedEvent("elic_done"));

    const block = useChatStore.getState().blocks[0];
    if (block?.type !== "elicitation") {
      throw new Error("expected an elicitation block");
    }
    expect(block.status).toBe("responded");
    expect(block.response).toEqual({ action: "accept", content: { answer: "yes" } });
  });

  it("is a no-op when no card matches the id", () => {
    // A late-arriving event for an elicitation the client never
    // saw (e.g. dropped during a brief SSE disconnect that
    // reconnected after the request was already resolved) is
    // discarded silently. The chat-store stays untouched.
    useChatStore.setState({ blocks: [elicitationBlock("elic_a")] });
    handleSessionEvent(elicitationResolvedEvent("elic_unknown"));
    const block = useChatStore.getState().blocks[0];
    if (block?.type !== "elicitation") {
      throw new Error("expected an elicitation block");
    }
    expect(block.status).toBe("pending");
    expect(block.response).toBeNull();
  });

  it("no-op when there are no elicitation blocks at all", () => {
    // The handler must not crash on streams with no approval
    // surface — the server publishes the event unconditionally
    // on every approval clear, even when no SSE subscriber has
    // an open chat for the session.
    useChatStore.setState({ blocks: [] });
    handleSessionEvent(elicitationResolvedEvent("elic_x"));
    expect(useChatStore.getState().blocks).toEqual([]);
  });
});

// Sticky-pref handoff: a sessions's snapshot trumps the store; an
// empty snapshot picks up the user's last compatible native pick.
// PATCH fires as a side effect so the next turn uses the override
// server-side.
describe("chatStore — bindStream sticky-pref handoff", () => {
  const CLAUDE_MODEL_OPTIONS = [
    {
      id: "opus",
      model: "system.ai.claude-opus-4-10",
      displayName: "Opus 4.10",
      isDefault: true,
    },
    {
      id: "sonnet",
      model: "system.ai.claude-sonnet-5",
      displayName: "Sonnet 5",
      isDefault: false,
    },
  ];

  interface SnapshotOverrides {
    labels?: Record<string, string>;
    reasoning_effort?: string | null;
    model_override?: string | null;
    cost_control_mode_override?: "on" | "off" | null;
    parent_session_id?: string | null;
    model_options?: Record<string, unknown>[];
  }

  /** Override the snapshot GET so a test can inject labels + overrides. */
  function withSnapshot(id: string, overrides: SnapshotOverrides): void {
    sessionLabels.set(id, { ...(overrides.labels ?? {}) });
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.split("?")[0] === `/v1/sessions/${id}` && (init?.method ?? "GET") === "GET") {
        return mockResponse({
          id,
          agent_id: "agent_xyz",
          status: "idle",
          created_at: 0,
          items: [],
          labels: overrides.labels ?? {},
          reasoning_effort: overrides.reasoning_effort ?? null,
          model_override: overrides.model_override ?? null,
          cost_control_mode_override: overrides.cost_control_mode_override ?? null,
          parent_session_id: overrides.parent_session_id ?? null,
          model_options: overrides.model_options ?? [],
        });
      }
      return defaultFetchHandler(input, init);
    });
  }

  function patchCallsFor(id: string): Record<string, unknown>[] {
    return fetchMock.mock.calls
      .filter(([u, init]) => {
        const url = typeof u === "string" ? u : u.toString();
        return (
          url === `/v1/sessions/${id}` && (init as RequestInit | undefined)?.method === "PATCH"
        );
      })
      .map(([, init]) => {
        const body = (init as RequestInit | undefined)?.body;
        return body ? (JSON.parse(body as string) as Record<string, unknown>) : {};
      });
  }

  it("applies sticky effort but never silently PATCHes the sticky model", async () => {
    // The sticky model is a UI preference: it must not be written onto the
    // session as a request the pane was never asked to honor.
    seedSession("conv_cn", []);
    withSnapshot("conv_cn", {
      labels: { "omnigent.wrapper": "claude-code-native-ui" },
      model_options: CLAUDE_MODEL_OPTIONS,
    });

    useChatStore.setState({
      selectedEffort: "high",
      selectedModel: "opus",
    });
    await useChatStore.getState().switchTo("conv_cn");

    const patches = patchCallsFor("conv_cn");
    expect(patches).toEqual(expect.arrayContaining([{ reasoning_effort: "high" }]));
    expect(patches.some((p) => "model_override" in p)).toBe(false);

    const state = useChatStore.getState();
    // The preference survives as a preference; no request was created.
    expect(state.selectedModel).toBe("opus");
    expect(state.sessionModelOverride).toBeNull();
    expect(state.selectedEffort).toBe("high");
  });

  // Flush the fire-and-forget sticky-apply promise chain (fetch → parse →
  // reject → catch → arm/clear the backoff) before the next assertion.
  const flushStickyApplies = () =>
    new Promise<void>((resolve) => {
      setTimeout(resolve, 0);
    });

  const nativeSnapshotResponse = (id: string): Response =>
    mockResponse({
      id,
      agent_id: "agent_xyz",
      status: "idle",
      created_at: 0,
      items: [],
      labels: { "omnigent.wrapper": "claude-code-native-ui" },
      reasoning_effort: null,
      model_override: null,
      model_options: CLAUDE_MODEL_OPTIONS,
    });

  it("stops re-firing sticky applies after a 5xx until the backoff clears", async () => {
    // Backend outage: every sticky-apply PATCH 500s. Without a client backoff
    // these re-fire on every bind/switch and pile onto the failing server.
    seedSession("conv_bo1", []);
    seedSession("conv_bo2", []);
    sessionLabels.set("conv_bo1", { "omnigent.wrapper": "claude-code-native-ui" });
    sessionLabels.set("conv_bo2", { "omnigent.wrapper": "claude-code-native-ui" });
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const path = (typeof input === "string" ? input : input.toString()).split("?")[0];
      const ours = path === "/v1/sessions/conv_bo1" || path === "/v1/sessions/conv_bo2";
      if (ours && init?.method === "PATCH") return mockResponse({}, { ok: false, status: 500 });
      if (ours && (init?.method ?? "GET") === "GET") {
        return nativeSnapshotResponse(path.slice("/v1/sessions/".length));
      }
      return defaultFetchHandler(input, init);
    });

    useChatStore.setState({ selectedEffort: "high", selectedModel: "opus" });

    // First bind fires the sticky applies; both 500 → the cooldown arms.
    await useChatStore.getState().switchTo("conv_bo1");
    await flushStickyApplies();
    expect(patchCallsFor("conv_bo1").length).toBeGreaterThan(0);

    // A second bind (different session) while the backend-wide cooldown holds
    // must NOT re-issue the silent applies.
    await useChatStore.getState().switchTo("conv_bo2");
    await flushStickyApplies();
    expect(patchCallsFor("conv_bo2")).toEqual([]);
    // ...and must NOT claim the sticky model as an override the skipped PATCH
    // never persisted — the /model readout stays honest during the cooldown.
    expect(conversationRegistry.peek("conv_bo2")!.getState().sessionModelOverride).toBeNull();
  });

  it("treats a 404 as a transient backend failure and pauses all sticky applies", async () => {
    // A 404 here means the permission check didn't succeed (a flaky permission
    // service), NOT that the session is gone — so it is backend-wide and
    // transient, and must pause every session's applies just like a 5xx rather
    // than singling out the session that happened to 404.
    seedSession("conv_p1", []);
    seedSession("conv_p2", []);
    sessionLabels.set("conv_p1", { "omnigent.wrapper": "claude-code-native-ui" });
    sessionLabels.set("conv_p2", { "omnigent.wrapper": "claude-code-native-ui" });
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const path = (typeof input === "string" ? input : input.toString()).split("?")[0];
      const ours = path === "/v1/sessions/conv_p1" || path === "/v1/sessions/conv_p2";
      if (ours && init?.method === "PATCH") return mockResponse({}, { ok: false, status: 404 });
      if (ours && (init?.method ?? "GET") === "GET") {
        return nativeSnapshotResponse(path.slice("/v1/sessions/".length));
      }
      return defaultFetchHandler(input, init);
    });

    useChatStore.setState({ selectedEffort: "high", selectedModel: "opus" });

    // First bind fires the sticky applies; both 404 → the cooldown arms.
    await useChatStore.getState().switchTo("conv_p1");
    await flushStickyApplies();
    expect(patchCallsFor("conv_p1").length).toBeGreaterThan(0);

    // A different session bound while the cooldown holds must NOT re-issue the
    // silent applies — the 404 pauses everyone, not just conv_p1.
    await useChatStore.getState().switchTo("conv_p2");
    await flushStickyApplies();
    expect(patchCallsFor("conv_p2")).toEqual([]);
  });

  it("does not reopen the cooldown when an intervening request succeeds", async () => {
    // The gate reopens by time, not on a success — otherwise the ~10% of
    // requests that get through mid-outage would flap it open and leak a fresh
    // apply each time. A successful bind here must leave the cooldown armed.
    seedSession("conv_f1", []);
    seedSession("conv_f2", []);
    sessionLabels.set("conv_f1", { "omnigent.wrapper": "claude-code-native-ui" });
    sessionLabels.set("conv_f2", { "omnigent.wrapper": "claude-code-native-ui" });
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const path = (typeof input === "string" ? input : input.toString()).split("?")[0];
      if (path === "/v1/sessions/conv_f1" && init?.method === "PATCH") {
        return mockResponse({}, { ok: false, status: 500 });
      }
      if (
        (path === "/v1/sessions/conv_f1" || path === "/v1/sessions/conv_f2") &&
        (init?.method ?? "GET") === "GET"
      ) {
        return nativeSnapshotResponse(path.slice("/v1/sessions/".length));
      }
      return defaultFetchHandler(input, init);
    });

    useChatStore.setState({ selectedEffort: "high", selectedModel: "opus" });

    // Arm the cooldown.
    await useChatStore.getState().switchTo("conv_f1");
    await flushStickyApplies();
    expect(patchCallsFor("conv_f1").length).toBeGreaterThan(0);

    // A brand-new send binds successfully (a request that got through) — but it
    // must NOT clear the cooldown.
    await useChatStore.getState().switchTo(null);
    await useChatStore.getState().send("hi", "agent_xyz");

    // A fresh native session is still suppressed: the success didn't flap it open.
    await useChatStore.getState().switchTo("conv_f2");
    await flushStickyApplies();
    expect(patchCallsFor("conv_f2")).toEqual([]);
  });

  it("reopens the cooldown once its window elapses", async () => {
    // Recovery is time-based: after the window the applies fire again (and stay
    // firing while the backend is healthy).
    const nowSpy = vi.spyOn(Date, "now").mockReturnValue(1_000);
    try {
      seedSession("conv_e1", []);
      seedSession("conv_e3", []);
      sessionLabels.set("conv_e1", { "omnigent.wrapper": "claude-code-native-ui" });
      sessionLabels.set("conv_e3", { "omnigent.wrapper": "claude-code-native-ui" });
      fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
        const path = (typeof input === "string" ? input : input.toString()).split("?")[0];
        if (path === "/v1/sessions/conv_e1" && init?.method === "PATCH") {
          return mockResponse({}, { ok: false, status: 500 });
        }
        if (
          (path === "/v1/sessions/conv_e1" || path === "/v1/sessions/conv_e3") &&
          (init?.method ?? "GET") === "GET"
        ) {
          return nativeSnapshotResponse(path.slice("/v1/sessions/".length));
        }
        return defaultFetchHandler(input, init);
      });

      useChatStore.setState({ selectedEffort: "high", selectedModel: "opus" });

      // Arm the cooldown at t=1s (window is 30s → reopens at t=31s).
      await useChatStore.getState().switchTo("conv_e1");
      await flushStickyApplies();
      expect(patchCallsFor("conv_e1").length).toBeGreaterThan(0);

      // Advance the clock past the window; a fresh session's applies fire again.
      nowSpy.mockReturnValue(32_000);
      await useChatStore.getState().switchTo("conv_e3");
      await flushStickyApplies();
      expect(patchCallsFor("conv_e3").length).toBeGreaterThan(0);
    } finally {
      nowSpy.mockRestore();
    }
  });

  it("lets only the newest model pick settle the persisted sticky preference", async () => {
    // A conversation-id guard can't order two picks. If A's PATCH is slow, the
    // user switches to B and picks again, then A resolves last: A's canonical
    // value overwrote the newer localStorage pref even though the UI correctly
    // showed B's. The wrong model then came back on reload / in a new chat.
    seedSession("conv_pick_a", []);
    seedSession("conv_pick_b", []);
    let releaseA: (() => void) | null = null;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/v1/sessions/conv_pick_a" && init?.method === "PATCH") {
        return new Promise<Response>((resolve) => {
          releaseA = () =>
            resolve(
              mockResponse({
                id: "conv_pick_a",
                agent_id: "agent_xyz",
                status: "idle",
                created_at: 0,
                items: [],
                model_override: "sonnet",
              }),
            );
        });
      }
      if (url === "/v1/sessions/conv_pick_b" && init?.method === "PATCH") {
        return mockResponse({
          id: "conv_pick_b",
          agent_id: "agent_xyz",
          status: "idle",
          created_at: 0,
          items: [],
          model_override: "opus",
        });
      }
      return defaultFetchHandler(input, init);
    });

    await useChatStore.getState().switchTo("conv_pick_a");
    const pickA = useChatStore.getState().setModel("sonnet");
    await tick();

    // Switch to B and pick a NEWER model, which settles first.
    await useChatStore.getState().switchTo("conv_pick_b");
    await useChatStore.getState().setModel("opus");
    expect(window.localStorage.getItem("omnigent.picker.model")).toBe("opus");

    // A's slower PATCH now resolves last. It must not revive the older pick.
    releaseA!();
    await pickA;

    expect(window.localStorage.getItem("omnigent.picker.model")).toBe("opus");
    expect(useChatStore.getState().selectedModel).toBe("opus");
    // A's own session override still settled to its canonical value.
    expect(conversationRegistry.peek("conv_pick_a")!.getState().sessionModelOverride).toBe(
      "sonnet",
    );
  });

  it("does not move the app-global picker when a bind lands after a switch away", async () => {
    // `selectedModel` / `selectedEffort` are app-global sticky picks, not
    // conversation state, so a cold bind that finishes in the background must
    // hydrate its OWN conversation without touching them — otherwise A's late
    // snapshot repaints the picker for whichever conversation the user is now
    // looking at (and two concurrent cold binds are last-response-wins).
    seedSession("conv_slow_bind", []);
    seedSession("conv_now_visible", []);
    let releaseSnapshot: (() => void) | null = null;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (
        url.split("?")[0] === "/v1/sessions/conv_slow_bind" &&
        (init?.method ?? "GET") === "GET" &&
        releaseSnapshot === null
      ) {
        return new Promise<Response>((resolve) => {
          releaseSnapshot = () =>
            resolve(
              mockResponse({
                id: "conv_slow_bind",
                agent_id: "agent_xyz",
                status: "idle",
                created_at: 0,
                items: [],
                labels: { "omnigent.wrapper": "claude-code-native-ui" },
                // The snapshot would hand "sonnet" to the picker.
                model_override: "sonnet",
                reasoning_effort: "low",
                model_options: CLAUDE_MODEL_OPTIONS,
              }),
            );
        });
      }
      return defaultFetchHandler(input, init);
    });

    useChatStore.setState({ selectedEffort: "high", selectedModel: "opus" });
    const binding = useChatStore.getState().switchTo("conv_slow_bind");
    await tick();
    // The user switches away before the snapshot lands.
    await useChatStore.getState().switchTo("conv_now_visible");
    releaseSnapshot!();
    await binding;

    // The visible conversation's picker is untouched...
    expect(useChatStore.getState().selectedModel).toBe("opus");
    expect(useChatStore.getState().selectedEffort).toBe("high");
    // ...while the backgrounded conversation still hydrated its own state.
    expect(conversationRegistry.peek("conv_slow_bind")!.getState().sessionModelOverride).toBe(
      "sonnet",
    );
  });

  it("keeps the sticky as a preference when delayed model options arrive", async () => {
    seedSession("conv_cn_delayed", []);
    window.localStorage.setItem("omnigent.picker.model", "opus");
    let snapshotCount = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (
        url.split("?")[0] === "/v1/sessions/conv_cn_delayed" &&
        (init?.method ?? "GET") === "GET"
      ) {
        snapshotCount += 1;
        return mockResponse({
          id: "conv_cn_delayed",
          agent_id: "agent_xyz",
          status: "idle",
          created_at: 0,
          items: [],
          labels: { "omnigent.wrapper": "claude-code-native-ui" },
          model_override: null,
          model_options: snapshotCount === 1 ? [] : CLAUDE_MODEL_OPTIONS,
        });
      }
      return defaultFetchHandler(input, init);
    });

    useChatStore.setState({ selectedEffort: null, selectedModel: "opus" });
    await useChatStore.getState().switchTo("conv_cn_delayed");

    expect(patchCallsFor("conv_cn_delayed").some((p) => "model_override" in p)).toBe(false);

    handleSessionEvent({
      type: "session_model_options",
      conversationId: "conv_cn_delayed",
    });
    await tick();

    // The catalog fills the picker; the sticky stays a preference — no
    // silent request is ever written on its behalf.
    expect(patchCallsFor("conv_cn_delayed").some((p) => "model_override" in p)).toBe(false);
    expect(useChatStore.getState()).toMatchObject({
      selectedModel: "opus",
      sessionModelOverride: null,
      codexModelOptions: CLAUDE_MODEL_OPTIONS,
    });
    window.localStorage.removeItem("omnigent.picker.model");
  });

  it("refetches a resolved Claude catalog when its event races the bind snapshot", async () => {
    seedSession("conv_cn_race", []);
    window.localStorage.setItem("omnigent.picker.model", "opus");
    let resolveInitialSnapshot: ((response: Response) => void) | null = null;
    let resolveItems: ((response: Response) => void) | null = null;
    let snapshotCount = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.startsWith("/v1/sessions/conv_cn_race/items")) {
        const response = defaultFetchHandler(input, init);
        return new Promise<Response>((resolve) => {
          resolveItems = resolve;
        }).then(() => response);
      }
      if (url.split("?")[0] === "/v1/sessions/conv_cn_race" && (init?.method ?? "GET") === "GET") {
        snapshotCount += 1;
        const response = mockResponse({
          id: "conv_cn_race",
          agent_id: "agent_xyz",
          status: "idle",
          created_at: 0,
          items: [],
          labels: { "omnigent.wrapper": "claude-code-native-ui" },
          model_override: null,
          model_options: snapshotCount === 1 ? [] : CLAUDE_MODEL_OPTIONS,
        });
        if (snapshotCount === 1) {
          return new Promise<Response>((resolve) => {
            resolveInitialSnapshot = resolve;
          });
        }
        return response;
      }
      return defaultFetchHandler(input, init);
    });

    useChatStore.setState({ selectedEffort: null, selectedModel: "opus" });
    const switchPromise = useChatStore.getState().switchTo("conv_cn_race");
    await tick();
    expect(resolveInitialSnapshot).not.toBeNull();

    handleSessionEvent({
      type: "session_model_options",
      conversationId: "conv_cn_race",
    });
    resolveInitialSnapshot!(
      mockResponse({
        id: "conv_cn_race",
        agent_id: "agent_xyz",
        status: "idle",
        created_at: 0,
        items: [],
        labels: { "omnigent.wrapper": "claude-code-native-ui" },
        model_override: null,
        model_options: [],
      }),
    );
    await tick();
    expect(snapshotCount).toBe(2);
    resolveItems!(mockResponse({}));
    await switchPromise;
    await tick();

    // The raced catalog still lands; the sticky stays a preference and no
    // silent request is written.
    expect(patchCallsFor("conv_cn_race").some((p) => "model_override" in p)).toBe(false);
    expect(useChatStore.getState()).toMatchObject({
      selectedModel: "opus",
      sessionModelOverride: null,
      codexModelOptions: CLAUDE_MODEL_OPTIONS,
    });
    window.localStorage.removeItem("omnigent.picker.model");
  });

  it("keeps a catalog-absent sticky as a preference when the catalog wins the race", async () => {
    // Same race as above, but the sticky pick ("fable") is absent from the
    // raced catalog. Display never derives from the sticky, so nothing needs
    // clearing — the preference survives and no request is written.
    seedSession("conv_cn_race_removed", []);
    window.localStorage.setItem("omnigent.picker.model", "fable");
    let resolveInitialSnapshot: ((response: Response) => void) | null = null;
    let resolveItems: ((response: Response) => void) | null = null;
    let snapshotCount = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.startsWith("/v1/sessions/conv_cn_race_removed/items")) {
        const response = defaultFetchHandler(input, init);
        return new Promise<Response>((resolve) => {
          resolveItems = resolve;
        }).then(() => response);
      }
      if (
        url.split("?")[0] === "/v1/sessions/conv_cn_race_removed" &&
        (init?.method ?? "GET") === "GET"
      ) {
        snapshotCount += 1;
        const response = mockResponse({
          id: "conv_cn_race_removed",
          agent_id: "agent_xyz",
          status: "idle",
          created_at: 0,
          items: [],
          labels: { "omnigent.wrapper": "claude-code-native-ui" },
          model_override: null,
          model_options: snapshotCount === 1 ? [] : CLAUDE_MODEL_OPTIONS,
        });
        if (snapshotCount === 1) {
          return new Promise<Response>((resolve) => {
            resolveInitialSnapshot = resolve;
          });
        }
        return response;
      }
      return defaultFetchHandler(input, init);
    });

    useChatStore.setState({ selectedEffort: null, selectedModel: "fable" });
    const switchPromise = useChatStore.getState().switchTo("conv_cn_race_removed");
    await tick();
    expect(resolveInitialSnapshot).not.toBeNull();

    handleSessionEvent({
      type: "session_model_options",
      conversationId: "conv_cn_race_removed",
    });
    resolveInitialSnapshot!(
      mockResponse({
        id: "conv_cn_race_removed",
        agent_id: "agent_xyz",
        status: "idle",
        created_at: 0,
        items: [],
        labels: { "omnigent.wrapper": "claude-code-native-ui" },
        model_override: null,
        model_options: [],
      }),
    );
    await tick();
    resolveItems!(mockResponse({}));
    await switchPromise;
    await tick();

    expect(patchCallsFor("conv_cn_race_removed").some((patch) => "model_override" in patch)).toBe(
      false,
    );
    expect(useChatStore.getState()).toMatchObject({
      selectedModel: "fable",
      sessionModelOverride: null,
      codexModelOptions: CLAUDE_MODEL_OPTIONS,
    });
    window.localStorage.removeItem("omnigent.picker.model");
  });

  it("does not apply a persisted Claude alias removed from the delayed catalog", async () => {
    seedSession("conv_cn_delayed_removed", []);
    window.localStorage.setItem("omnigent.picker.model", "fable");
    let snapshotCount = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (
        url.split("?")[0] === "/v1/sessions/conv_cn_delayed_removed" &&
        (init?.method ?? "GET") === "GET"
      ) {
        snapshotCount += 1;
        return mockResponse({
          id: "conv_cn_delayed_removed",
          agent_id: "agent_xyz",
          status: "idle",
          created_at: 0,
          items: [],
          labels: { "omnigent.wrapper": "claude-code-native-ui" },
          model_override: null,
          model_options: snapshotCount === 1 ? [] : CLAUDE_MODEL_OPTIONS,
        });
      }
      return defaultFetchHandler(input, init);
    });

    useChatStore.setState({ selectedEffort: null, selectedModel: "fable" });
    await useChatStore.getState().switchTo("conv_cn_delayed_removed");
    handleSessionEvent({
      type: "session_model_options",
      conversationId: "conv_cn_delayed_removed",
    });
    await tick();

    expect(
      patchCallsFor("conv_cn_delayed_removed").some((patch) => "model_override" in patch),
    ).toBe(false);
    expect(useChatStore.getState().sessionModelOverride).toBeNull();
    window.localStorage.removeItem("omnigent.picker.model");
  });

  it("does NOT apply a sticky model to a routing-enabled session", async () => {
    // Intelligent routing owns model selection: the bind-time sticky handoff
    // must NOT silently re-pin the last-used model, or the server's
    // `model_override is None` routing guard would skip and the judge would
    // never run. (This is the claude-native repro: new chat + routing on.)
    seedSession("conv_routing", []);
    withSnapshot("conv_routing", {
      labels: { "omnigent.wrapper": "claude-code-native-ui" },
      cost_control_mode_override: "on",
      model_override: null,
    });

    useChatStore.setState({ selectedModel: "claude-opus-4-7" });
    await useChatStore.getState().switchTo("conv_routing");

    // No model_override PATCH fired (contrast the handoff test above).
    const patches = patchCallsFor("conv_routing");
    expect(patches.some((p) => "model_override" in p)).toBe(false);
    // …and the session is not mislabeled as pinned to the sticky model.
    expect(useChatStore.getState().sessionModelOverride).toBeNull();
  });

  it("applies sticky effort but never the sticky model on a codex-native session", async () => {
    seedSession("conv_codex", []);
    withSnapshot("conv_codex", {
      labels: { "omnigent.wrapper": "codex-native-ui" },
      model_options: [
        {
          id: "gpt-5.4",
          model: "databricks-gpt-5-4",
          displayName: "GPT-5.4",
          defaultReasoningEffort: "high",
          supportedReasoningEfforts: [
            { reasoningEffort: "low", description: "Low" },
            { reasoningEffort: "medium", description: "Medium" },
            { reasoningEffort: "high", description: "High" },
            { reasoningEffort: "xhigh", description: "Extra high" },
          ],
          isDefault: false,
        },
      ],
    });

    useChatStore.setState({
      selectedEffort: "xhigh",
      selectedModel: "gpt-5.4",
    });
    await useChatStore.getState().switchTo("conv_codex");

    const patches = patchCallsFor("conv_codex");
    expect(patches).toEqual(expect.arrayContaining([{ reasoning_effort: "xhigh" }]));
    expect(patches.some((p) => "model_override" in p)).toBe(false);

    const state = useChatStore.getState();
    expect(state.selectedModel).toBe("gpt-5.4");
    expect(state.sessionModelOverride).toBeNull();
    expect(state.selectedEffort).toBe("xhigh");
  });

  it("does NOT apply sticky effort or model to a sub-agent (child) session", async () => {
    // Observer sticky prefs must not overwrite child sessions.
    seedSession("conv_child", []);
    withSnapshot("conv_child", {
      labels: { "omnigent.wrapper": "claude-code-native-ui" },
      parent_session_id: "conv_parent",
      model_options: CLAUDE_MODEL_OPTIONS,
    });

    useChatStore.setState({
      selectedEffort: "xhigh",
      selectedModel: "opus",
    });
    await useChatStore.getState().switchTo("conv_child");

    const patches = patchCallsFor("conv_child");
    expect(patches.some((p) => "reasoning_effort" in p)).toBe(false);
    expect(patches.some((p) => "model_override" in p)).toBe(false);

    // Sticky prefs are preserved for later top-level sessions.
    const state = useChatStore.getState();
    expect(state.selectedEffort).toBe("xhigh");
    expect(state.selectedModel).toBe("opus");
  });

  it("does NOT PATCH a non-Claude sticky model onto a claude-native session", async () => {
    // Regression: `selectedModel` is a single global pick shared across
    // harnesses, so it can hold a Codex default like `gpt-5.4`. Handing
    // that to a claude-native session would persist model_override=gpt-5.4
    // and launch Claude Code with `--model gpt-5.4`, which it can't run.
    // The handoff must skip a non-Claude model and leave the session on its
    // own default (model_override untouched).
    seedSession("conv_cn_gpt", []);
    withSnapshot("conv_cn_gpt", { labels: { "omnigent.wrapper": "claude-code-native-ui" } });

    useChatStore.setState({ selectedEffort: null, selectedModel: "gpt-5.4" });
    await useChatStore.getState().switchTo("conv_cn_gpt");

    const patches = patchCallsFor("conv_cn_gpt");
    expect(patches.some((p) => "model_override" in p)).toBe(false);
  });

  it("does NOT PATCH a removed Claude alias onto a new session", async () => {
    seedSession("conv_cn_removed", []);
    withSnapshot("conv_cn_removed", {
      labels: { "omnigent.wrapper": "claude-code-native-ui" },
      model_options: CLAUDE_MODEL_OPTIONS,
    });

    useChatStore.setState({ selectedEffort: null, selectedModel: "fable" });
    await useChatStore.getState().switchTo("conv_cn_removed");

    expect(patchCallsFor("conv_cn_removed").some((p) => "model_override" in p)).toBe(false);
    expect(useChatStore.getState().sessionModelOverride).toBeNull();
  });

  it("does NOT PATCH a non-Codex sticky model onto a codex-native session", async () => {
    // Same guard in the opposite direction: a Claude alias from the global
    // picker must not be handed to Codex app-server as its next-turn model.
    seedSession("conv_codex_claude", []);
    withSnapshot("conv_codex_claude", { labels: { "omnigent.wrapper": "codex-native-ui" } });

    useChatStore.setState({ selectedEffort: null, selectedModel: "opus" });
    await useChatStore.getState().switchTo("conv_codex_claude");

    const patches = patchCallsFor("conv_codex_claude");
    expect(patches.some((p) => "model_override" in p)).toBe(false);
  });

  it("shows a claude-native session's stamped effort and does not overwrite it", async () => {
    seedSession("conv_cn_eff", []);
    withSnapshot("conv_cn_eff", {
      labels: { "omnigent.wrapper": "claude-code-native-ui" },
      reasoning_effort: "medium",
    });

    useChatStore.setState({ selectedEffort: "high", selectedModel: null });
    await useChatStore.getState().switchTo("conv_cn_eff");

    const patches = patchCallsFor("conv_cn_eff");
    // Existing server effort wins over the sticky picker value.
    expect(patches.some((p) => "reasoning_effort" in p)).toBe(false);
    expect(useChatStore.getState().selectedEffort).toBe("medium");
  });

  it("does NOT PATCH model or effort on a custom session even with sticky prefs", async () => {
    seedSession("conv_other", []);
    // No terminal/native labels → custom web agent.
    withSnapshot("conv_other", { labels: {} });

    useChatStore.setState({
      selectedEffort: "high",
      selectedModel: "claude-opus-4-7",
    });
    await useChatStore.getState().switchTo("conv_other");

    const patches = patchCallsFor("conv_other");
    expect(patches.some((p) => "reasoning_effort" in p)).toBe(false);
    expect(patches.some((p) => "model_override" in p)).toBe(false);

    const state = useChatStore.getState();
    // Sticky picks remain in the store, but were not applied here.
    expect(state.selectedModel).toBe("claude-opus-4-7");
    expect(state.selectedEffort).toBe("high");
  });

  it("does NOT PATCH effort on an active custom session", async () => {
    seedSession("conv_custom", []);
    withSnapshot("conv_custom", { labels: {} });
    await useChatStore.getState().switchTo("conv_custom");
    fetchMock.mockClear();

    await useChatStore.getState().setEffort("high");

    expect(patchCallsFor("conv_custom")).toEqual([]);
    expect(useChatStore.getState().selectedEffort).toBe("high");
  });

  it("PATCHes effort on an active claude-native session", async () => {
    seedSession("conv_supported", []);
    withSnapshot("conv_supported", { labels: { "omnigent.wrapper": "claude-code-native-ui" } });
    await useChatStore.getState().switchTo("conv_supported");
    fetchMock.mockClear();

    await useChatStore.getState().setEffort("high");

    expect(patchCallsFor("conv_supported")).toEqual([{ reasoning_effort: "high" }]);
    expect(useChatStore.getState().selectedEffort).toBe("high");
  });

  it("PATCHes effort on an active codex-native session", async () => {
    seedSession("conv_codex_supported", []);
    withSnapshot("conv_codex_supported", { labels: { "omnigent.wrapper": "codex-native-ui" } });
    await useChatStore.getState().switchTo("conv_codex_supported");
    fetchMock.mockClear();

    await useChatStore.getState().setEffort("high");

    expect(patchCallsFor("conv_codex_supported")).toEqual([{ reasoning_effort: "high" }]);
    expect(useChatStore.getState().selectedEffort).toBe("high");
  });

  it("hydrates Codex Plan mode from the session label", async () => {
    seedSession("conv_plan", []);
    withSnapshot("conv_plan", {
      labels: {
        "omnigent.wrapper": "codex-native-ui",
        "omnigent.codex_native.collaboration_mode": "plan",
      },
    });

    await useChatStore.getState().switchTo("conv_plan");

    expect(useChatStore.getState().codexPlanMode).toBe(true);
  });

  it("ignores Codex Plan mode labels on non-Codex sessions", async () => {
    seedSession("conv_not_codex_plan", []);
    withSnapshot("conv_not_codex_plan", {
      labels: {
        "omnigent.codex_native.collaboration_mode": "plan",
      },
    });

    await useChatStore.getState().switchTo("conv_not_codex_plan");

    expect(useChatStore.getState().codexPlanMode).toBe(false);
  });

  it("PATCHes Codex Plan mode and settles from the returned labels", async () => {
    seedSession("conv_plan_toggle", []);
    withSnapshot("conv_plan_toggle", { labels: { "omnigent.wrapper": "codex-native-ui" } });
    await useChatStore.getState().switchTo("conv_plan_toggle");
    fetchMock.mockClear();

    await useChatStore.getState().setCodexPlanMode(true);

    expect(patchCallsFor("conv_plan_toggle")).toEqual([{ collaboration_mode: "plan" }]);
    expect(useChatStore.getState().codexPlanMode).toBe(true);
  });

  it("rolls back Codex Plan mode when the PATCH is rejected", async () => {
    seedSession("conv_plan_failure", []);
    withSnapshot("conv_plan_failure", { labels: { "omnigent.wrapper": "codex-native-ui" } });
    await useChatStore.getState().switchTo("conv_plan_failure");
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const path = url.split("?")[0]!;
      if (path === "/v1/sessions/conv_plan_failure" && init?.method === "PATCH") {
        return mockResponse(
          {
            error: {
              code: "runner_unavailable",
              message: "Could not enter Plan mode: no live Codex runner is available.",
            },
          },
          { ok: false, status: 503 },
        );
      }
      return defaultFetchHandler(input, init);
    });
    fetchMock.mockClear();

    await expect(useChatStore.getState().setCodexPlanMode(true)).rejects.toThrow(
      "Could not enter Plan mode",
    );

    expect(patchCallsFor("conv_plan_failure")).toEqual([{ collaboration_mode: "plan" }]);
    expect(useChatStore.getState().codexPlanMode).toBe(false);
    expect(sessionLabels.get("conv_plan_failure")).not.toHaveProperty(
      "omnigent.codex_native.collaboration_mode",
    );
  });

  it("server-side overrides win over sticky pref and skip the PATCH", async () => {
    seedSession("conv_existing", []);
    withSnapshot("conv_existing", {
      labels: { "omnigent.wrapper": "claude-code-native-ui" },
      reasoning_effort: "low",
      model_override: "claude-sonnet-4-6",
    });

    useChatStore.setState({
      selectedEffort: "high",
      selectedModel: "claude-opus-4-7",
    });
    await useChatStore.getState().switchTo("conv_existing");

    const patches = patchCallsFor("conv_existing");
    // Only the runner_id PATCH should fire — no sticky handoff because
    // the session already carries authoritative values.
    expect(patches.some((p) => "reasoning_effort" in p)).toBe(false);
    expect(patches.some((p) => "model_override" in p)).toBe(false);

    const state = useChatStore.getState();
    expect(state.selectedEffort).toBe("low");
    // The sticky preference is untouched by the session's own request…
    expect(state.selectedModel).toBe("claude-opus-4-7");
    // …which rides in verbatim as the request slot.
    expect(state.sessionModelOverride).toBe("claude-sonnet-4-6");
  });

  it("does NOT surface an unapplied sticky model as the session override (custom session)", async () => {
    // Regression: a fresh non-claude-native session inherits the global
    // sticky pick into `selectedModel`, but the pick is NOT applied
    // server-side. `/model` reads `sessionModelOverride`, which must stay
    // null so the readout shows "agent default" rather than a bogus
    // "(override)". See ChatPage `/model` and `/context` readouts.
    seedSession("conv_sticky_custom", []);
    withSnapshot("conv_sticky_custom", { labels: {} });

    useChatStore.setState({ selectedModel: "claude-sonnet-4-6", sessionModelOverride: null });
    await useChatStore.getState().switchTo("conv_sticky_custom");

    const patches = patchCallsFor("conv_sticky_custom");
    expect(patches.some((p) => "model_override" in p)).toBe(false);

    const state = useChatStore.getState();
    // Sticky pick preserved for cross-session restore...
    expect(state.selectedModel).toBe("claude-sonnet-4-6");
    // ...but it is NOT the session's active override.
    expect(state.sessionModelOverride).toBeNull();
  });

  it("never surfaces the sticky as the session override (claude-native)", async () => {
    // The sticky is a UI preference, not a request: binding a session must
    // neither PATCH it nor present it as the session's active override.
    seedSession("conv_sticky_cn", []);
    withSnapshot("conv_sticky_cn", {
      labels: { "omnigent.wrapper": "claude-code-native-ui" },
      model_options: CLAUDE_MODEL_OPTIONS,
    });

    useChatStore.setState({ selectedModel: "opus", sessionModelOverride: null });
    await useChatStore.getState().switchTo("conv_sticky_cn");

    expect(patchCallsFor("conv_sticky_cn").some((p) => "model_override" in p)).toBe(false);
    expect(useChatStore.getState().sessionModelOverride).toBeNull();
  });

  it("does NOT surface a non-Claude sticky model as the session override (claude-native)", async () => {
    // The handoff skips a non-Claude sticky pick (Claude Code can't run
    // it), so it never becomes the session override.
    seedSession("conv_sticky_gpt", []);
    withSnapshot("conv_sticky_gpt", { labels: { "omnigent.wrapper": "claude-code-native-ui" } });

    useChatStore.setState({ selectedModel: "gpt-5.4", sessionModelOverride: null });
    await useChatStore.getState().switchTo("conv_sticky_gpt");

    expect(patchCallsFor("conv_sticky_gpt").some((p) => "model_override" in p)).toBe(false);
    expect(useChatStore.getState().sessionModelOverride).toBeNull();
  });
});

describe("chatStore — pumpStreamEvents frame batching", () => {
  /** A 35-char delta (> the reducer's 30-char flush threshold) so each
   * one flushes exactly one text_chunk; `marker` makes chunks orderable. */
  function delta(marker: string): string {
    return sse("response.output_text.delta", { delta: `${marker.repeat(34)} ` });
  }

  /** A FrameScheduler whose pending flush the test fires by hand. */
  function manualScheduler(): {
    scheduler: FrameScheduler;
    fire: () => void;
    pending: () => boolean;
  } {
    let cb: (() => void) | null = null;
    return {
      scheduler: {
        schedule: (c) => {
          cb = c;
        },
        cancel: () => {
          cb = null;
        },
      },
      fire: () => {
        const c = cb;
        cb = null;
        if (c) c();
      },
      pending: () => cb !== null,
    };
  }

  const setState = useChatStore.setState as unknown as Parameters<typeof pumpStreamEvents>[3];
  const getState = useChatStore.getState as unknown as Parameters<typeof pumpStreamEvents>[4];

  it("coalesces multiple buffered blocks into one in-order append per frame", async () => {
    useChatStore.setState({ conversationId: "conv_batch", blocks: [] });
    const sink = pushableStream();
    const controller = new AbortController();
    const manual = manualScheduler();
    void pumpStreamEvents(
      "conv_batch",
      sink.stream,
      controller,
      setState,
      getState,
      manual.scheduler,
    );

    // Open the response and stream three chunks. The first content block
    // flushes synchronously (snappy first paint); the next two buffer.
    sink.push(sse("response.created", { id: "resp_b", status: "in_progress", output: [] }));
    sink.push(delta("A"));
    sink.push(delta("B"));
    sink.push(delta("C"));
    await tick();

    // Only response_start + the first chunk have committed; B and C wait
    // in the frame buffer (proves they were NOT flushed one-per-block).
    const mid = useChatStore.getState().blocks;
    const midChunks = mid.filter((b) => b.type === "text_chunk");
    // 1 = first-content sync flush only. If 3, batching is broken (each
    // chunk committed immediately); if 0, the first-paint flush is broken.
    expect(midChunks).toHaveLength(1);
    expect(manual.pending()).toBe(true);

    // Fire one frame → B and C land together, after A, in arrival order.
    manual.fire();
    const after = useChatStore.getState().blocks;
    const chunkText = (b: AnyBlock) => (b.type === "text_chunk" ? b.text.trim()[0] : null);
    const order = after.map(chunkText).filter((c): c is string => c !== null);
    // Exactly A,B,C in order — coalesced append preserved arrival order.
    expect(order).toEqual(["A", "B", "C"]);

    controller.abort();
  });

  it("appends live command output to the running tool card", async () => {
    useChatStore.setState({ conversationId: "conv_tool_delta", blocks: [] });
    const sink = pushableStream();
    const controller = new AbortController();
    void pumpStreamEvents("conv_tool_delta", sink.stream, controller, setState, getState);

    sink.push(sse("response.created", { id: "resp_tool", status: "in_progress", output: [] }));
    sink.push(
      sse("response.output_item.done", {
        item: {
          id: "item_call",
          type: "function_call",
          response_id: "resp_tool",
          call_id: "call_123",
          name: "shell",
          arguments: '{"command":"pytest"}',
          status: "in_progress",
        },
      }),
    );
    sink.push(
      sse("response.function_call_output.delta", {
        call_id: "call_123",
        delta: "collecting ",
      }),
    );
    sink.push(
      sse("response.function_call_output.delta", {
        call_id: "call_123",
        delta: "tests...",
      }),
    );
    await tick();

    const group = useChatStore
      .getState()
      .blocks.find((b): b is ToolGroup => b.type === "tool_group");
    expect(group?.executions[0]?.output).toBe("collecting tests...");

    controller.abort();
  });

  it("keeps response_start/response_end lifecycle intact through batching", async () => {
    useChatStore.setState({
      conversationId: "conv_life",
      blocks: [],
      activeResponse: null,
      status: "idle",
    });
    const sink = pushableStream();
    const controller = new AbortController();
    const manual = manualScheduler();
    void pumpStreamEvents(
      "conv_life",
      sink.stream,
      controller,
      setState,
      getState,
      manual.scheduler,
    );

    sink.push(sse("response.created", { id: "resp_life", status: "in_progress", output: [] }));
    sink.push(delta("Z"));
    await tick();
    // response_start landed and flipped the store into the streaming
    // lifecycle synchronously (not deferred to a frame).
    expect(useChatStore.getState().activeResponse).toEqual({
      responseId: "resp_life",
      state: "streaming",
      error: null,
    });
    expect(useChatStore.getState().status).toBe("streaming");

    sink.push(
      sse("response.completed", {
        id: "resp_life",
        status: "completed",
        output: [
          {
            type: "message",
            role: "assistant",
            content: [{ type: "output_text", text: "Z done" }],
          },
        ],
      }),
    );
    sink.close();
    await tick();
    await tick();

    const blocks = useChatStore.getState().blocks;
    // response_end is force-flushed before the terminal side effects, so
    // the lifecycle markers bracket the content in order.
    expect(blocks[0]!.type).toBe("response_start");
    expect(blocks.at(-1)!.type).toBe("response_end");
    // activeResponse settled to completed and the send flag cleared —
    // the response_end branch ran after the buffer was flushed.
    expect(useChatStore.getState().activeResponse?.state).toBe("completed");
    expect(useChatStore.getState().status).toBe("idle");

    controller.abort();
  });

  it("a stale wrapper response_end does NOT downgrade a newer turn's activeResponse", async () => {
    // Hermes-native first-turn spinner bug: the runner opens an empty wrapper
    // response that completes AFTER the forwarder's per-turn id (hermes_turn_1)
    // has already taken over activeResponse. The wrapper's response_end must be
    // ignored for lifecycle — otherwise it finalizes the LIVE turn to
    // "completed", stopping its tool cards streaming (the "no spinner" bug).
    useChatStore.setState({
      conversationId: "conv_stale",
      blocks: [],
      activeResponse: null,
      status: "idle",
      sessionStatus: "idle",
    });
    const sink = pushableStream();
    const controller = new AbortController();
    const manual = manualScheduler();
    void pumpStreamEvents(
      "conv_stale",
      sink.stream,
      controller,
      setState,
      getState,
      manual.scheduler,
    );

    // 1) The empty wrapper response opens → activeResponse adopts its id.
    sink.push(sse("response.created", { id: "resp_wrap", status: "in_progress", output: [] }));
    await tick();
    expect(useChatStore.getState().activeResponse).toEqual({
      responseId: "resp_wrap",
      state: "streaming",
      error: null,
    });

    // 2) The forwarder's per-turn running edge takes over activeResponse.
    handleSessionEvent({
      type: "session_status",
      conversationId: "conv_stale",
      status: "running",
      responseId: "hermes_turn_1",
    });
    expect(useChatStore.getState().activeResponse).toEqual({
      responseId: "hermes_turn_1",
      state: "streaming",
      error: null,
    });

    // 3) The stale wrapper completes (empty). Its response_end targets
    //    resp_wrap, which is no longer the active response, so it must NOT
    //    touch activeResponse.
    sink.push(sse("response.completed", { id: "resp_wrap", status: "completed", output: [] }));
    sink.close();
    await tick();
    await tick();

    // The live turn is still streaming — its cards keep their spinner.
    expect(useChatStore.getState().activeResponse).toEqual({
      responseId: "hermes_turn_1",
      state: "streaming",
      error: null,
    });

    controller.abort();
  });

  it("dedupes a stream block whose itemId already exists (snapshot collision)", async () => {
    // Seed a block as if the snapshot hydrated item "fc_dup" already.
    const seeded: AnyBlock = {
      type: "text_done",
      ctx: { agent: null, depth: 0, turn: 0, timestamp: 0, responseId: "resp_d", itemId: "fc_dup" },
      fullText: "seeded",
      hasCodeBlocks: false,
    };
    useChatStore.setState({ conversationId: "conv_dup", blocks: [seeded] });
    const sink = pushableStream();
    const controller = new AbortController();
    const manual = manualScheduler();
    void pumpStreamEvents(
      "conv_dup",
      sink.stream,
      controller,
      setState,
      getState,
      manual.scheduler,
    );

    // Stream a function_call output item carrying the SAME item id.
    sink.push(sse("response.created", { id: "resp_d", status: "in_progress", output: [] }));
    sink.push(
      sse("response.output_item.done", {
        item: {
          type: "function_call",
          id: "fc_dup",
          call_id: "call_1",
          name: "DoThing",
          arguments: "{}",
          response_id: "resp_d",
        },
      }),
    );
    await tick();
    manual.fire();

    const withId = useChatStore.getState().blocks.filter((b) => b.ctx.itemId === "fc_dup");
    // Still exactly one block for "fc_dup": the stream copy was skipped.
    // If 2, the snapshot-collision dedup regressed and the tool would
    // render twice.
    expect(withId).toHaveLength(1);
    expect(withId[0]!.type).toBe("text_done");

    controller.abort();
  });

  it("stamps a persisted message's id onto the streamed text in the buffer instead of duplicating it", async () => {
    // Regression: the relay persists each streamed text segment at a
    // tool-call boundary and publishes it as output_item.done(message)
    // so clients learn its store-assigned id (#3146). The tool_call has
    // already closed the streamed text id-less by then, so the
    // reducer's open-section dedupe can't catch the event — the pump
    // must match it back to the streamed text_done by content and stamp
    // the id in place, not append a second copy.
    useChatStore.setState({ conversationId: "conv_stamp", blocks: [] });
    const sink = pushableStream();
    const controller = new AbortController();
    const manual = manualScheduler();
    void pumpStreamEvents(
      "conv_stamp",
      sink.stream,
      controller,
      setState,
      getState,
      manual.scheduler,
    );

    const segment = "Got it — dispatching to all three vendors now.";
    sink.push(sse("response.created", { id: "resp_s", status: "in_progress", output: [] }));
    sink.push(sse("response.output_text.delta", { delta: segment }));
    // claude-sdk tool call: the reducer closes the streamed text
    // (id-less text_done) and yields the tool group.
    sink.push(
      sse("response.output_item.done", {
        item: {
          type: "function_call",
          id: "fc_s",
          call_id: "call_s",
          name: "sys_session_send",
          arguments: "{}",
          response_id: "resp_s",
        },
      }),
    );
    // The relay's mid-turn flush publish: the persisted copy of the
    // exact text that just streamed.
    sink.push(
      sse("response.output_item.done", {
        item: {
          id: "msg_seg1",
          response_id: "resp_s",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [{ type: "output_text", text: segment }],
          model: "polly",
        },
      }),
    );
    await tick();
    manual.fire();

    const blocks = useChatStore.getState().blocks;
    const dones = blocks.filter((b) => b.type === "text_done");
    // Exactly one rendered copy of the segment. If 2, the mid-turn
    // duplication regressed: the persisted copy appended next to the
    // streamed one ("◆ agent + same text" after the tool call).
    expect(dones).toHaveLength(1);
    expect(dones[0]!.type === "text_done" && dones[0]!.fullText).toBe(segment);
    // The streamed block now carries the persisted item's id, so
    // reconnect reconciliation (itemId-keyed) won't splice the
    // persisted copy in as a duplicate — the original #3146 hole.
    expect(dones[0]!.ctx.itemId).toBe("msg_seg1");
    // The text keeps its streamed position ABOVE the tool call; an
    // appended copy would render below it.
    const types = blocks.map((b) => b.type);
    expect(types.indexOf("text_done")).toBeLessThan(types.indexOf("tool_group"));

    controller.abort();
  });

  it("stamps the persisted message's id onto streamed text already committed to the store", async () => {
    // Same scenario as above, but the frame flush fires BETWEEN the
    // tool call and the persisted-message publish, so the id-less
    // streamed text_done is already in state.blocks (not the frame
    // buffer) when the message event arrives. The pump must stamp it
    // in place via the store path.
    useChatStore.setState({ conversationId: "conv_stamp2", blocks: [] });
    const sink = pushableStream();
    const controller = new AbortController();
    const manual = manualScheduler();
    void pumpStreamEvents(
      "conv_stamp2",
      sink.stream,
      controller,
      setState,
      getState,
      manual.scheduler,
    );

    const segment = "All three workers are live. Dispatching now.";
    sink.push(sse("response.created", { id: "resp_s2", status: "in_progress", output: [] }));
    sink.push(sse("response.output_text.delta", { delta: segment }));
    sink.push(
      sse("response.output_item.done", {
        item: {
          type: "function_call",
          id: "fc_s2",
          call_id: "call_s2",
          name: "sys_os_shell",
          arguments: "{}",
          response_id: "resp_s2",
        },
      }),
    );
    await tick();
    // Commit the buffered id-less text_done + tool_group to the store
    // before the persisted-message event arrives.
    manual.fire();
    const committedAt = useChatStore
      .getState()
      .blocks.findIndex((b) => b.type === "text_done" && !b.ctx.itemId);
    // Setup check: the streamed text committed id-less (the state the
    // relay's publish must reconcile against).
    expect(committedAt).toBeGreaterThanOrEqual(0);

    sink.push(
      sse("response.output_item.done", {
        item: {
          id: "msg_seg2",
          response_id: "resp_s2",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [{ type: "output_text", text: segment }],
          model: "polly",
        },
      }),
    );
    await tick();
    manual.fire();

    const blocks = useChatStore.getState().blocks;
    const dones = blocks.filter((b) => b.type === "text_done");
    // One copy, stamped in place at its original index — not appended.
    expect(dones).toHaveLength(1);
    expect(dones[0]!.ctx.itemId).toBe("msg_seg2");
    expect(blocks.findIndex((b) => b.type === "text_done")).toBe(committedAt);

    controller.abort();
  });

  it("appends a persisted message whose text never streamed (non-streamed harness)", async () => {
    // The stamp path must not swallow real content: a message item
    // whose text has no streamed counterpart (no matching id-less
    // text_done) is new content and must render.
    useChatStore.setState({ conversationId: "conv_nostream", blocks: [] });
    const sink = pushableStream();
    const controller = new AbortController();
    const manual = manualScheduler();
    void pumpStreamEvents(
      "conv_nostream",
      sink.stream,
      controller,
      setState,
      getState,
      manual.scheduler,
    );

    sink.push(sse("response.created", { id: "resp_ns", status: "in_progress", output: [] }));
    sink.push(
      sse("response.output_item.done", {
        item: {
          id: "msg_ns",
          response_id: "resp_ns",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [{ type: "output_text", text: "Non-streamed reply." }],
          model: "polly",
        },
      }),
    );
    await tick();
    manual.fire();

    const dones = useChatStore.getState().blocks.filter((b) => b.type === "text_done");
    // The message rendered (no streamed copy existed to stamp). If 0,
    // the stamp branch wrongly consumed a message with no streamed
    // counterpart and dropped real content.
    expect(dones).toHaveLength(1);
    expect(dones[0]!.type === "text_done" && dones[0]!.fullText).toBe("Non-streamed reply.");
    expect(dones[0]!.ctx.itemId).toBe("msg_ns");

    controller.abort();
  });

  it("drops a buffered block whose item a snapshot merge committed before the flush", async () => {
    useChatStore.setState({ conversationId: "conv_bufdup", blocks: [] });
    const sink = pushableStream();
    const controller = new AbortController();
    const manual = manualScheduler();
    void pumpStreamEvents(
      "conv_bufdup",
      sink.stream,
      controller,
      setState,
      getState,
      manual.scheduler,
    );

    // First content flushes synchronously; the function_call lands in the
    // frame buffer, where the push-time dedupe has already passed it
    // (nothing was committed under "fc_buf" yet).
    sink.push(sse("response.created", { id: "resp_bd", status: "in_progress", output: [] }));
    sink.push(delta("A"));
    sink.push(
      sse("response.output_item.done", {
        item: {
          type: "function_call",
          id: "fc_buf",
          call_id: "call_b",
          name: "DoThing",
          arguments: "{}",
          response_id: "resp_bd",
        },
      }),
    );
    await tick();
    expect(manual.pending()).toBe(true);
    expect(useChatStore.getState().blocks.some((b) => b.ctx.itemId === "fc_buf")).toBe(false);

    // A snapshot merge (bind hydration / reconnect reconcile) commits the
    // same item while it sits in the buffer — merges read only
    // `state.blocks`, so they cannot see the buffered copy.
    const merged: AnyBlock = {
      type: "text_done",
      ctx: {
        agent: null,
        depth: 0,
        turn: 0,
        timestamp: 0,
        responseId: "resp_bd",
        itemId: "fc_buf",
      },
      fullText: "merged copy",
      hasCodeBlocks: false,
    };
    useChatStore.setState((s) => ({ blocks: [...s.blocks, merged] }));

    // The flush must re-check at commit time and drop the buffered copy —
    // without that, "fc_buf" renders twice (merge insert + buffer append).
    manual.fire();
    const withId = useChatStore.getState().blocks.filter((b) => b.ctx.itemId === "fc_buf");
    expect(withId).toHaveLength(1);
    // The committed copy is the merge's, proving the buffered one was dropped.
    expect(withId[0]!.type).toBe("text_done");

    controller.abort();
  });

  it("flushes the buffered tail on clean EOF without a response_end", async () => {
    useChatStore.setState({ conversationId: "conv_eof", blocks: [] });
    const sink = pushableStream();
    const controller = new AbortController();
    const manual = manualScheduler();
    const done = pumpStreamEvents(
      "conv_eof",
      sink.stream,
      controller,
      setState,
      getState,
      manual.scheduler,
    );

    // Stream content, then close WITHOUT a response.completed — models an
    // idle proxy disconnect mid-response. B and C are buffered (A flushed
    // synchronously as first content) and no frame is ever fired.
    sink.push(sse("response.created", { id: "resp_eof", status: "in_progress", output: [] }));
    sink.push(delta("A"));
    sink.push(delta("B"));
    sink.push(delta("C"));
    await tick();
    expect(useChatStore.getState().blocks.filter((b) => b.type === "text_chunk")).toHaveLength(1);
    sink.close();
    await done;

    // The clean-EOF flush committed the buffered tail — all three chunks
    // present in order. If the buffer were just dropped in `finally`, B
    // and C (the last tokens of the turn) would be silently lost.
    const order = useChatStore
      .getState()
      .blocks.filter((b): b is Extract<AnyBlock, { type: "text_chunk" }> => b.type === "text_chunk")
      .map((b) => b.text.trim()[0]);
    expect(order).toEqual(["A", "B", "C"]);
  });

  it("lands a pending frame on its OWN conversation, never the visible one", async () => {
    // A buffered frame that fires after the user switches away must not paint
    // onto the newly-visible conversation. That used to be enforced by flush()
    // bailing on a conversationId mismatch — and it dropped the blocks. Now the
    // buffer belongs to conv_old's entry, so the blocks are kept AND the visible
    // conversation is untouched.
    const { set: setOld, get: getOld } = bindConversationForTest("conv_old");
    const sink = pushableStream();
    const controller = new AbortController();
    const manual = manualScheduler();
    void pumpStreamEvents("conv_old", sink.stream, controller, setOld, getOld, manual.scheduler);

    sink.push(sse("response.created", { id: "resp_old", status: "in_progress", output: [] }));
    sink.push(delta("A")); // first content → sync flush onto conv_old
    sink.push(delta("B")); // buffered, frame pending
    sink.push(delta("C")); // buffered, frame pending
    await tick();
    expect(manual.pending()).toBe(true);

    // The user navigates to another conversation. conv_old keeps its stream.
    bindConversationForTest("conv_new");

    manual.fire();
    expect(useChatStore.getState().conversationId).toBe("conv_new");
    // conv_new is untouched...
    expect(useChatStore.getState().blocks).toEqual([]);
    // ...and conv_old kept its own buffered output rather than discarding it.
    expect(getOld().blocks.length).toBeGreaterThan(0);
  });
});

describe("chatStore — pumpStreamEvents end reasons", () => {
  const setState = useChatStore.setState as unknown as Parameters<typeof pumpStreamEvents>[3];
  const getState = useChatStore.getState as unknown as Parameters<typeof pumpStreamEvents>[4];
  // Commit synchronously so each pushed frame lands without waiting a frame.
  const immediate: FrameScheduler = { schedule: (cb) => cb(), cancel: () => {} };

  it("returns 'server_closed' when the stream ends with the [DONE] sentinel", async () => {
    useChatStore.setState({ conversationId: "conv_done", blocks: [] });
    const sink = pushableStream();
    const done = pumpStreamEvents(
      "conv_done",
      sink.stream,
      new AbortController(),
      setState,
      getState,
      immediate,
    );
    sink.push("data: [DONE]\n\n");
    sink.close();
    // [DONE] = deliberate server close → the loop must NOT reconnect.
    expect(await done).toBe("server_closed");
  });

  it("returns 'dropped' when the byte stream closes without [DONE]", async () => {
    useChatStore.setState({ conversationId: "conv_eof2", blocks: [] });
    const sink = pushableStream();
    const done = pumpStreamEvents(
      "conv_eof2",
      sink.stream,
      new AbortController(),
      setState,
      getState,
      immediate,
    );
    sink.close();
    // Bare close (idle proxy FIN) reads as a transport drop → reconnectable.
    expect(await done).toBe("dropped");
  });

  it("returns 'dropped' when the stream errors (e.g. net::ERR_HTTP2_PROTOCOL_ERROR)", async () => {
    useChatStore.setState({ conversationId: "conv_err", blocks: [] });
    const sink = pushableStream();
    const done = pumpStreamEvents(
      "conv_err",
      sink.stream,
      new AbortController(),
      setState,
      getState,
      immediate,
    );
    sink.error(new Error("net::ERR_HTTP2_PROTOCOL_ERROR"));
    expect(await done).toBe("dropped");
  });

  it("leaves session lifecycle untouched on a dropped connection", async () => {
    // Model a turn in flight. The pump must NOT flip it to failed on a
    // drop — the reconnect loop owns lifecycle, so a recycle stays silent.
    useChatStore.setState({
      conversationId: "conv_keep",
      blocks: [],
      status: "streaming",
      sessionStatus: "running",
      activeResponse: { responseId: "resp_k", state: "streaming", error: null },
    });
    const sink = pushableStream();
    const done = pumpStreamEvents(
      "conv_keep",
      sink.stream,
      new AbortController(),
      setState,
      getState,
      immediate,
    );
    sink.error();
    expect(await done).toBe("dropped");
    const st = useChatStore.getState();
    expect(st.sessionStatus).toBe("running");
    expect(st.status).toBe("streaming");
    expect(st.activeResponse).toEqual({ responseId: "resp_k", state: "streaming", error: null });
  });

  it("keeps pumping when the user switches to another conversation", async () => {
    // Switching away used to end the pump ("switched"), because one shared
    // `blocks` array meant a background pump would corrupt the visible
    // transcript. Each conversation now owns its state, so backgrounding is not
    // a reason to stop — it is the entire feature. Only disposal ends the pump.
    const { set: setA, get: getA } = bindConversationForTest("conv_sw_a");
    const sink = pushableStream();
    const done = pumpStreamEvents(
      "conv_sw_a",
      sink.stream,
      new AbortController(),
      setA,
      getA,
      immediate,
    );
    sink.push(sse("response.created", { id: "resp_sw", status: "in_progress", output: [] }));
    await tick();
    bindConversationForTest("conv_sw_b");
    // A 34-char + trailing-space delta flushes a chunk, so the next for-await
    // iteration runs the pump's liveness guard — which conv_sw_a still passes.
    sink.push(sse("response.output_text.delta", { delta: `${"z".repeat(34)} ` }));
    await tick();
    // Still running: the delta landed on conv_sw_a, not on the visible one.
    expect(getA().blocks.length).toBeGreaterThan(0);
    expect(useChatStore.getState().blocks).toEqual([]);
    sink.push("data: [DONE]\n\n");
    sink.close();
    expect(await done).toBe("server_closed");
  });

  it("returns 'switched' once its conversation is evicted", async () => {
    // The end reason survives for the case it now describes: the entry is gone
    // (evicted or deleted), so there is nowhere to write and nothing to reopen.
    const { set: setA, get: getA } = bindConversationForTest("conv_sw_gone");
    const sink = pushableStream();
    const done = pumpStreamEvents(
      "conv_sw_gone",
      sink.stream,
      new AbortController(),
      setA,
      getA,
      immediate,
    );
    sink.push(sse("response.created", { id: "resp_sw", status: "in_progress", output: [] }));
    await tick();
    releaseConversation("conv_sw_gone");
    sink.push(sse("response.output_text.delta", { delta: `${"z".repeat(34)} ` }));
    expect(await done).toBe("switched");
  });

  it("returns 'aborted' when the controller aborts mid-stream", async () => {
    const controller = new AbortController();
    useChatStore.setState({ conversationId: "conv_abrt2", blocks: [] });
    const sink = pushableStream();
    const done = pumpStreamEvents(
      "conv_abrt2",
      sink.stream,
      controller,
      setState,
      getState,
      immediate,
    );
    sink.push(sse("response.created", { id: "resp_abrt", status: "in_progress", output: [] }));
    await tick();
    controller.abort();
    sink.push(sse("response.output_text.delta", { delta: `${"z".repeat(34)} ` }));
    expect(await done).toBe("aborted");
  });
});

describe("chatStore — startStreamPump reconnect loop", () => {
  const setState = useChatStore.setState as unknown as Parameters<typeof startStreamPump>[2];
  const getState = useChatStore.getState as unknown as Parameters<typeof startStreamPump>[3];

  /** Route `/stream` opens to controllable sinks; everything else hits the
   *  default handler. Returns the growing list of opened stream sinks. */
  function routeStreamOpens(): StreamSink[] {
    const sinks: StreamSink[] = [];
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (/\/v1\/sessions\/[^/]+\/stream$/.test(url)) {
        const sink = pushableStream();
        sinks.push(sink);
        return mockResponse(null, { bodyStream: sink.stream });
      }
      return defaultFetchHandler(input, init);
    });
    return sinks;
  }

  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("reopens the stream after a transport drop, then stops on [DONE]", async () => {
    seedSession("conv_loop", []);
    const sinks = routeStreamOpens();
    const controller = new AbortController();
    useChatStore.setState({ conversationId: "conv_loop", abortController: controller });

    const loop = startStreamPump("conv_loop", controller, setState, getState);
    await vi.advanceTimersByTimeAsync(1);
    expect(sinks).toHaveLength(1);

    // Drop without [DONE] → the loop re-subscribes instantly (no backoff
    // after a healthy connection). Exactly one new open; if 1, reconnect
    // never fired; if >2, it spun.
    sinks[0]!.error();
    await vi.advanceTimersByTimeAsync(6000);
    expect(sinks).toHaveLength(2);

    // A deliberate [DONE] close ends the loop — no further reopen.
    const last = sinks[sinks.length - 1]!;
    last.push("data: [DONE]\n\n");
    last.close();
    await vi.advanceTimersByTimeAsync(6000);
    const afterDone = sinks.length;
    await vi.advanceTimersByTimeAsync(12000);
    expect(sinks.length).toBe(afterDone);

    controller.abort();
    await loop;
  });

  it("stops reconnecting (and clears the binding) when aborted after a drop", async () => {
    seedSession("conv_abrt3", []);
    const sinks = routeStreamOpens();
    const controller = new AbortController();
    useChatStore.setState({ conversationId: "conv_abrt3", abortController: controller });

    const loop = startStreamPump("conv_abrt3", controller, setState, getState);
    await vi.advanceTimersByTimeAsync(1);
    expect(sinks).toHaveLength(1);

    sinks[0]!.error(); // drop → loop enters backoff
    controller.abort(); // user navigates away before the reconnect fires
    await vi.advanceTimersByTimeAsync(6000);
    await loop;

    expect(sinks).toHaveLength(1); // no reconnect after abort
    expect(useChatStore.getState().abortController).toBeNull();
  });

  it("gives up after exhausting the transient-404 retry cap", async () => {
    seedSession("conv_404", []);
    let opens = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (/\/v1\/sessions\/[^/]+\/stream$/.test(url)) {
        opens += 1;
        return mockResponse({}, { ok: false, status: 404 });
      }
      return defaultFetchHandler(input, init);
    });
    const controller = new AbortController();
    useChatStore.setState({
      conversationId: "conv_404",
      abortController: controller,
      sessionStatus: "running",
    });

    const loop = startStreamPump("conv_404", controller, setState, getState);
    // Every open 404s, so each is retried with backoff until the cap is
    // exhausted; advance well past the worst-case cumulative backoff so the
    // loop has time to give up on its own.
    await vi.advanceTimersByTimeAsync(90_000);
    await loop;

    expect(opens).toBe(11); // 1 initial open + 10 retries, then gives up
    // Giving up on OUR stream says nothing about what the agent is doing, so
    // the session's own status is left alone — only the server may declare a
    // session failed. The dropped stream surfaces as offline liveness.
    expect(useChatStore.getState().sessionStatus).toBe("running");
    expect(useChatStore.getState().abortController).toBeNull();
  });

  it("rides out a handful of transient 404s (backend restart) without failing the session", async () => {
    seedSession("conv_404flap", []);
    const sinks: StreamSink[] = [];
    let opens = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (/\/v1\/sessions\/[^/]+\/stream$/.test(url)) {
        opens += 1;
        // A reverse proxy 404s the first few opens (backend mid-restart);
        // the next one succeeds once it's back.
        if (opens <= 3) return mockResponse({}, { ok: false, status: 404 });
        const sink = pushableStream();
        sinks.push(sink);
        return mockResponse(null, { bodyStream: sink.stream });
      }
      return defaultFetchHandler(input, init);
    });
    const controller = new AbortController();
    useChatStore.setState({
      conversationId: "conv_404flap",
      abortController: controller,
      sessionStatus: "running",
    });

    const loop = startStreamPump("conv_404flap", controller, setState, getState);
    await vi.advanceTimersByTimeAsync(20_000);

    expect(opens).toBe(4);
    expect(sinks).toHaveLength(1);
    // The 404s resolved well inside the cap — must not flip to failed.
    expect(useChatStore.getState().sessionStatus).toBe("running");

    sinks[0]!.push("data: [DONE]\n\n");
    sinks[0]!.close();
    await vi.advanceTimersByTimeAsync(1);
    await loop;
  });

  it("treats the first SUCCESSFUL open as initial (no reconcile) even after a failed open", async () => {
    seedSession("conv_failopen", []);
    const sinks: StreamSink[] = [];
    let opens = 0;
    let reconcileFetches = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (/\/v1\/sessions\/[^/]+\/stream$/.test(url)) {
        opens += 1;
        // First open fails transiently (500 → retry); the second succeeds.
        if (opens === 1) return mockResponse({}, { ok: false, status: 500 });
        const sink = pushableStream();
        sinks.push(sink);
        return mockResponse(null, { bodyStream: sink.stream });
      }
      // reconcileOnReconnect is the only thing in startStreamPump that GETs
      // the session snapshot / items page; count those to detect it running.
      if (
        /\/v1\/sessions\/[^/]+\/items/.test(url) ||
        (/\/v1\/sessions\/[^/]+$/.test(url) && (init?.method ?? "GET") === "GET")
      ) {
        reconcileFetches += 1;
      }
      return defaultFetchHandler(input, init);
    });
    const controller = new AbortController();
    useChatStore.setState({ conversationId: "conv_failopen", abortController: controller });

    const loop = startStreamPump("conv_failopen", controller, setState, getState);
    // First open 500s, backs off, second open succeeds.
    await vi.advanceTimersByTimeAsync(6000);
    expect(sinks).toHaveLength(1);
    // The recovered first connection is INITIAL, not a reconnect, so
    // reconcileOnReconnect must not have fired. If a failed open had been
    // mis-counted as a prior connection, this would be >= 1.
    expect(reconcileFetches).toBe(0);

    // Clean up: a [DONE] close ends the loop.
    sinks[0]!.push("data: [DONE]\n\n");
    sinks[0]!.close();
    await vi.advanceTimersByTimeAsync(1);
    await loop;
  });

  it("recycles a byte-silent stream via the stall guard and reconciles the gap", async () => {
    const preGap = [userMessage("stall_pre", "before the stall")];
    seedSession("conv_stall", preGap);
    const sinks = routeStreamOpens();
    const controller = new AbortController();
    useChatStore.setState({
      conversationId: "conv_stall",
      abortController: controller,
      blocks: itemsToBlocks(preGap),
    });

    const loop = startStreamPump("conv_stall", controller, setState, getState);
    await drainAsync();
    expect(sinks).toHaveLength(1);

    // An item commits while the socket is half-open: no bytes, no close.
    const gapItem = userMessage("stall_gap", "committed during the stall");
    seedSessionItems("conv_stall", [...preGap, gapItem]);

    // Nothing arrives for the full stall window. The guard must declare
    // the transport dead and the loop must re-subscribe — a hung read
    // never ends on its own, so pre-guard this tab stayed frozen forever.
    await vi.advanceTimersByTimeAsync(SSE_STALL_TIMEOUT_MS + 1_000);
    await drainAsync();
    expect(sinks).toHaveLength(2);

    // The reconnect reconciled the committed snapshot: the gap item is in.
    expect(useChatStore.getState().blocks.map((b) => b.ctx.itemId)).toContain(gapItem.id);

    const last = sinks[1]!;
    last.push("data: [DONE]\n\n");
    last.close();
    await drainAsync(2);
    await loop;
  });

  it("keeps a heartbeat-alive stream connected across many stall windows", async () => {
    seedSession("conv_hb", []);
    const sinks = routeStreamOpens();
    const controller = new AbortController();
    useChatStore.setState({ conversationId: "conv_hb", abortController: controller });

    const loop = startStreamPump("conv_hb", controller, setState, getState);
    await drainAsync();
    expect(sinks).toHaveLength(1);

    // Several stall windows of wall clock, with a heartbeat inside each
    // one: the guard must keep re-arming rather than tripping.
    /* oxlint-disable no-await-in-loop */
    for (let i = 0; i < 8; i += 1) {
      sinks[0]!.push(sse("session.heartbeat", {}));
      await vi.advanceTimersByTimeAsync(20_000);
    }
    /* oxlint-enable no-await-in-loop */
    expect(sinks).toHaveLength(1);

    sinks[0]!.push("data: [DONE]\n\n");
    sinks[0]!.close();
    await drainAsync(2);
    await loop;
  });

  it("backs off when the stream open answers with a non-SSE content type", async () => {
    seedSession("conv_html", []);
    let opens = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (/\/v1\/sessions\/[^/]+\/stream$/.test(url)) {
        opens += 1;
        // An expired auth ingress: the fetch follows its redirect to a 200
        // text/html login page instead of the SSE body.
        const html = pushableStream();
        html.push("<!doctype html><title>Login</title>");
        html.close();
        return mockResponse(null, { bodyStream: html.stream, contentType: "text/html" });
      }
      return defaultFetchHandler(input, init);
    });
    const controller = new AbortController();
    useChatStore.setState({ conversationId: "conv_html", abortController: controller });

    const loop = startStreamPump("conv_html", controller, setState, getState);
    await vi.advanceTimersByTimeAsync(6_000);

    // Rejected as a failed open → retried WITH backoff. Pumping the HTML
    // instead would read as a clean drop and reconnect with no delay — a
    // hot loop hammering the login page (dozens of opens in this window).
    expect(opens).toBeGreaterThan(1);
    expect(opens).toBeLessThan(10);

    controller.abort();
    await vi.advanceTimersByTimeAsync(6_000);
    await loop;
  });

  it("recycles a stale stream on tab-visible / network-online, but never a fresh one", async () => {
    seedSession("conv_wake", []);
    // Sinks that honor the fetch abort signal like the real fetch does:
    // aborting the attempt severs the in-flight body read.
    const sinks: StreamSink[] = [];
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (/\/v1\/sessions\/[^/]+\/stream$/.test(url)) {
        const sink = pushableStream();
        sinks.push(sink);
        init?.signal?.addEventListener("abort", () =>
          sink.error(new DOMException("aborted", "AbortError")),
        );
        return mockResponse(null, { bodyStream: sink.stream });
      }
      return defaultFetchHandler(input, init);
    });
    const controller = new AbortController();
    useChatStore.setState({ conversationId: "conv_wake", abortController: controller });

    const loop = startStreamPump("conv_wake", controller, setState, getState);
    await drainAsync();
    expect(sinks).toHaveLength(1);

    // Fresh bytes → a visibility return must NOT churn the connection.
    sinks[0]!.push(sse("session.heartbeat", {}));
    await drainAsync();
    document.dispatchEvent(new Event("visibilitychange"));
    await drainAsync();
    expect(sinks).toHaveLength(1);

    // Silent past the stale window but before the stall guard's own
    // ceiling: the wake check must recycle NOW, not wait out the guard.
    await vi.advanceTimersByTimeAsync(SSE_STALE_RECYCLE_MS + 1_000);
    document.dispatchEvent(new Event("visibilitychange"));
    await drainAsync();
    expect(sinks).toHaveLength(2);

    // Same staleness discovered via the browser regaining network.
    await vi.advanceTimersByTimeAsync(SSE_STALE_RECYCLE_MS + 1_000);
    window.dispatchEvent(new Event("online"));
    await drainAsync();
    expect(sinks).toHaveLength(3);

    const last = sinks[2]!;
    last.push("data: [DONE]\n\n");
    last.close();
    await drainAsync(2);
    await loop;
  });

  /** Drain reconcile's sequential await chain under fake timers. */
  async function drainAsync(turns = 25): Promise<void> {
    /* oxlint-disable no-await-in-loop */
    for (let i = 0; i < turns; i += 1) {
      await vi.advanceTimersByTimeAsync(1);
    }
    /* oxlint-enable no-await-in-loop */
  }

  function gapUser(prefix: string, idx: number): ConversationItem {
    return userMessage(`${prefix}_${idx.toString().padStart(4, "0")}`, `${prefix} ${idx}`);
  }

  it("backfills a multi-page disconnect gap until it reaches the rendered transcript", async () => {
    // 30 committed items; the rendered window is the newest 20.
    const preGap = Array.from({ length: 30 }, (_, i) => gapUser("pre", i));
    const windowItems = preGap.slice(-SESSION_HISTORY_PAGE_SIZE);
    seedSession("conv_gap", preGap);
    const sinks = routeStreamOpens();
    const controller = new AbortController();
    useChatStore.setState({
      conversationId: "conv_gap",
      abortController: controller,
      blocks: itemsToBlocks(windowItems),
      hasMoreHistory: true,
      oldestItemId: windowItems[0]!.id,
    });

    const loop = startStreamPump("conv_gap", controller, setState, getState);
    await drainAsync();
    expect(sinks).toHaveLength(1);

    // 45 items (> 2 pages) commit while the stream is down.
    const gap = Array.from({ length: 45 }, (_, i) => gapUser("gap", i));
    seedSessionItems("conv_gap", [...preGap, ...gap]);
    sinks[0]!.error();
    await drainAsync();
    expect(sinks).toHaveLength(2);

    const state = useChatStore.getState();
    // Every gap item is present, in order, after the window — the single
    // newest page covers only the last 20, so pre-fix the 25 oldest gap
    // items would sit in a hole no code path could ever fetch.
    expect(state.blocks.map((b) => b.ctx.itemId)).toEqual([
      ...windowItems.map((item) => item.id),
      ...gap.map((item) => item.id),
    ]);
    // Backfill is not a re-hydrate: the scroll-up cursor is untouched.
    expect(state.oldestItemId).toBe(windowItems[0]!.id);
    expect(state.hasMoreHistory).toBe(true);

    const last = sinks[1]!;
    last.push("data: [DONE]\n\n");
    last.close();
    await drainAsync(2);
    await loop;
  });

  it("re-hydrates the window when the gap outruns the backfill page cap", async () => {
    const preGap = Array.from({ length: 30 }, (_, i) => gapUser("pre", i));
    const windowItems = preGap.slice(-SESSION_HISTORY_PAGE_SIZE);
    seedSession("conv_bigap", preGap);
    const sinks = routeStreamOpens();
    const controller = new AbortController();
    useChatStore.setState({
      conversationId: "conv_bigap",
      abortController: controller,
      blocks: itemsToBlocks(windowItems),
      hasMoreHistory: true,
      oldestItemId: windowItems[0]!.id,
    });

    const loop = startStreamPump("conv_bigap", controller, setState, getState);
    await drainAsync();
    expect(sinks).toHaveLength(1);

    // 100 gap items: 4 backfill pages (80) can't reach the rendered
    // transcript, so reconcile must fall back to a fresh window.
    const gap = Array.from({ length: 100 }, (_, i) => gapUser("gap", i));
    seedSessionItems("conv_bigap", [...preGap, ...gap]);
    sinks[0]!.error();
    await drainAsync();
    expect(sinks).toHaveLength(2);

    const state = useChatStore.getState();
    // The window was replaced wholesale with one fresh window fetch, exactly
    // as a cold bind loads it — not left with a mid-transcript hole, and not
    // paged in over several requests (which would shift the transcript under
    // a reader who never asked for this).
    expect(state.blocks.map((b) => b.ctx.itemId)).toEqual(
      gap.slice(-INITIAL_WINDOW_ITEMS).map((item) => item.id),
    );
    // The cursor was rewound to the fresh window's top, so everything older
    // (the pre-gap transcript included) is reachable again by paging up.
    expect(state.oldestItemId).toBe(gap.at(-INITIAL_WINDOW_ITEMS)!.id);
    expect(state.hasMoreHistory).toBe(true);

    // Scroll-up still pages from the window's top, one page at a time.
    await useChatStore.getState().loadMoreHistory();
    expect(useChatStore.getState().blocks.map((b) => b.ctx.itemId)).toEqual(
      [...preGap.slice(-SESSION_HISTORY_PAGE_SIZE), ...gap].map((item) => item.id),
    );

    const last = sinks[1]!;
    last.push("data: [DONE]\n\n");
    last.close();
    await drainAsync(2);
    await loop;
  });

  it("splices the active turn's gap items after its already-rendered blocks", async () => {
    const toolItem = (id: string): ConversationItem => ({
      id,
      response_id: "resp_T",
      type: "mcp_call",
      status: "completed",
      name: "run_step",
      data: {},
    });
    const u1 = userMessage("resp_T", "kick off");
    const t1 = toolItem("tool_pre");
    const t2 = toolItem("tool_gap_1");
    const t3 = toolItem("tool_gap_2");
    // Server state: the streaming turn committed t2/t3 during the gap.
    seedSession("conv_turngap", [u1, t1, t2, t3]);
    const sinks = routeStreamOpens();
    const controller = new AbortController();
    useChatStore.setState({
      conversationId: "conv_turngap",
      abortController: controller,
      // Pre-gap render: the turn's prompt + its first tool card.
      blocks: itemsToBlocks([u1, t1]),
      activeResponse: { responseId: "resp_T", state: "streaming", error: null },
      status: "streaming",
    });

    const loop = startStreamPump("conv_turngap", controller, setState, getState);
    await drainAsync();
    expect(sinks).toHaveLength(1);
    sinks[0]!.error();
    await drainAsync();
    expect(sinks).toHaveLength(2);

    const state = useChatStore.getState();
    // The gap items belong to the ACTIVE turn and are NEWER than its
    // already-rendered blocks, so they land after t1 — the pre-fix splice
    // put them before the turn's first block, i.e. above u1/t1.
    expect(state.blocks.map((b) => b.ctx.itemId)).toEqual([u1.id, t1.id, t2.id, t3.id]);
    // The turn ended during the gap (snapshot status "idle"): the spinner
    // recovery still applies on this path.
    expect(state.activeResponse?.state).toBe("completed");
    expect(state.status).toBe("idle");

    const last = sinks[1]!;
    last.push("data: [DONE]\n\n");
    last.close();
    await drainAsync(2);
    await loop;
  });

  it("keeps reconciling a conversation the user navigated away from", async () => {
    // This used to assert the opposite: switch-away destroyed the window, so a
    // reconcile still in flight was stale and had to be discarded. A background
    // conversation now keeps its window AND must keep reconciling — the ingress
    // recycles every stream ~every 5 minutes, and the server keeps no replay
    // buffer, so skipping the repair would leave it silently missing events.
    const preGap = Array.from({ length: 30 }, (_, i) => gapUser("spre", i));
    const windowItems = preGap.slice(-SESSION_HISTORY_PAGE_SIZE);
    seedSession("conv_stale", preGap);
    seedSession("conv_other", []);
    const sinks: StreamSink[] = [];
    let releaseBackfill: (() => void) | null = null;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (/\/v1\/sessions\/[^/]+\/stream$/.test(url)) {
        const sink = pushableStream();
        sinks.push(sink);
        return mockResponse(null, { bodyStream: sink.stream });
      }
      if (/\/v1\/sessions\/conv_stale\/items\?.*after=/.test(url) && releaseBackfill === null) {
        return new Promise<Response>((resolve) => {
          releaseBackfill = () => resolve(defaultFetchHandler(input, init));
        });
      }
      return defaultFetchHandler(input, init);
    });
    const controller = new AbortController();
    const { set: setStale, get: getStale } = bindConversationForTest("conv_stale", {
      abortController: controller,
      blocks: itemsToBlocks(windowItems),
      hasMoreHistory: true,
      oldestItemId: windowItems[0]!.id,
    });

    const loop = startStreamPump("conv_stale", controller, setStale, getStale);
    await drainAsync();
    expect(sinks).toHaveLength(1);

    // A drop mid-turn: the reconnect reconciles, and parks on its first
    // deferred backfill page.
    const gap = Array.from({ length: 100 }, (_, i) => gapUser("sgap", i));
    seedSessionItems("conv_stale", [...preGap, ...gap]);
    sinks[0]!.error();
    await drainAsync();
    expect(releaseBackfill).not.toBeNull();

    // The user leaves while that reconcile is still in flight.
    await useChatStore.getState().switchTo("conv_other");
    await drainAsync(5);

    // It resolves against the backgrounded conversation, which is still live.
    releaseBackfill!();
    await drainAsync();

    // The visible conversation is untouched...
    expect(useChatStore.getState().conversationId).toBe("conv_other");
    expect(useChatStore.getState().blocks).toEqual([]);
    // ...and conv_stale picked up the items it missed during the gap, so
    // returning to it shows a current transcript rather than one frozen at the
    // moment the stream dropped. The 100-item gap exceeds the backfill cap, so
    // the window is replaced wholesale rather than extended — what matters is
    // that it now ends at the newest item, not at the pre-gap tail.
    const staleIds = getStale().blocks.map((b) => b.ctx.itemId);
    expect(staleIds.at(-1)).toBe(gap.at(-1)!.id);
    expect(staleIds).not.toContain(windowItems.at(-1)!.id);

    // Release the conversation so the reconnect loop stops: a live entry keeps
    // reconnecting by design, which is exactly what this test asserts.
    releaseConversation("conv_stale");
    sinks[1]!.error();
    await drainAsync(2);
    await loop;
  });

  it("re-hydrate keeps pending elicitation and synthetic error blocks in the tail", async () => {
    const preGap = Array.from({ length: 30 }, (_, i) => gapUser("kpre", i));
    const windowItems = preGap.slice(-SESSION_HISTORY_PAGE_SIZE);
    seedSession("conv_keep", preGap);
    // The prompt is still parked server-side, so the snapshot lists it —
    // a card absent from `pending_elicitations` would (correctly) be
    // flipped to "Resolved elsewhere" by the elicitation reconcile
    // instead of staying answerable.
    seedPendingElicitations("conv_keep", [
      {
        type: "response.elicitation_request",
        elicitation_id: "elic_keep",
        params: {
          mode: "form",
          message: "Allow the tool call?",
          requestedSchema: null,
          phase: "tool_call",
          policy_name: "ask",
          content_preview: "",
        },
      },
    ]);
    const sinks = routeStreamOpens();
    const controller = new AbortController();
    // A cold-load-replayed approval card and a bind-time synthetic error:
    // both itemId-less with responseId "" (no streaming rid owns them).
    const approvalCard: ElicitationBlock = {
      type: "elicitation",
      ctx: { agent: null, depth: 0, turn: 0, timestamp: 0, responseId: "", itemId: null },
      elicitationId: "elic_keep",
      message: "Allow the tool call?",
      phase: "tool_call",
      policyName: "ask",
      contentPreview: "",
      requestedSchema: {},
      url: null,
      status: "pending",
      response: null,
    };
    const syntheticError: ErrorBlock = {
      type: "error",
      ctx: { agent: null, depth: 0, turn: 0, timestamp: 0, responseId: "", itemId: null },
      message: "boom",
      source: "",
      code: "task_failed",
    };
    useChatStore.setState({
      conversationId: "conv_keep",
      abortController: controller,
      blocks: [...itemsToBlocks(windowItems), approvalCard, syntheticError],
      hasMoreHistory: true,
      oldestItemId: windowItems[0]!.id,
    });

    const loop = startStreamPump("conv_keep", controller, setState, getState);
    await drainAsync();
    expect(sinks).toHaveLength(1);

    // 100 gap items: the backfill cap is outrun, forcing the re-hydrate.
    const gap = Array.from({ length: 100 }, (_, i) => gapUser("kgap", i));
    seedSessionItems("conv_keep", [...preGap, ...gap]);
    sinks[0]!.error();
    await drainAsync();
    expect(sinks).toHaveLength(2);

    const state = useChatStore.getState();
    // The fresh fetch returns ITEMS only — dropping these blocks would lose
    // the pending ApprovalCard (and the failure reason) with no way back.
    expect(state.blocks.map((b) => b.ctx.itemId ?? b.type)).toEqual([
      ...gap.slice(-INITIAL_WINDOW_ITEMS).map((item) => item.id),
      "elicitation",
      "error",
    ]);
    const card = state.blocks.find((b): b is ElicitationBlock => b.type === "elicitation");
    expect(card?.elicitationId).toBe("elic_keep");
    expect(card?.status).toBe("pending");
    const err = state.blocks.find((b): b is ErrorBlock => b.type === "error");
    expect(err?.message).toBe("boom");

    const last = sinks[1]!;
    last.push("data: [DONE]\n\n");
    last.close();
    await drainAsync(2);
    await loop;
  });

  /** A native message-scoped live delta frame (`message_id` + chunk index). */
  function nativeDeltaFrame(messageId: string, index: number, delta: string): string {
    return sse("response.output_text.delta", { delta, message_id: messageId, index });
  }

  /** The `live:<messageId>` provisional preview blocks currently rendered. */
  function livePreviews(): Extract<AnyBlock, { type: "text_done" }>[] {
    return useChatStore
      .getState()
      .blocks.filter(
        (b): b is Extract<AnyBlock, { type: "text_done" }> =>
          b.type === "text_done" && (b.ctx.itemId?.startsWith("live:") ?? false),
      );
  }

  it("rebuilds a native live preview from the cumulative replay instead of appending", async () => {
    seedSession("conv_native_replay", []);
    const sinks = routeStreamOpens();
    const controller = new AbortController();
    useChatStore.setState({ conversationId: "conv_native_replay", abortController: controller });

    const loop = startStreamPump("conv_native_replay", controller, setState, getState);
    await drainAsync();
    expect(sinks).toHaveLength(1);

    sinks[0]!.push(nativeDeltaFrame("m1", 0, "Hello "));
    sinks[0]!.push(nativeDeltaFrame("m1", 1, "wor"));
    await drainAsync();
    expect(livePreviews().map((b) => b.fullText)).toEqual(["Hello wor"]);

    // Drop mid-message. On the next connection the server replays the
    // message as ONE cumulative delta — the joined text so far at its
    // highest index, including the "ld!" chunk that fired into the dead
    // socket (see inflight_text.snapshot_for).
    sinks[0]!.error();
    await drainAsync();
    expect(sinks).toHaveLength(2);
    sinks[1]!.push(nativeDeltaFrame("m1", 2, "Hello world!"));
    await drainAsync();

    const previews = livePreviews();
    // Exactly one preview holding exactly the replayed text.
    // "Hello worHello world!" = the pre-fix bug: the pre-drop preview
    // survived the reconnect and the fresh pump appended the cumulative
    // replay onto it.
    expect(previews.map((b) => b.fullText)).toEqual(["Hello world!"]);
    expect(previews[0]!.ctx.itemId).toBe("live:m1");

    const last = sinks[1]!;
    last.push("data: [DONE]\n\n");
    last.close();
    await drainAsync(2);
    await loop;
  });

  it("drops a native live preview whose message committed during the gap", async () => {
    seedSession("conv_native_commit", []);
    const sinks = routeStreamOpens();
    const controller = new AbortController();
    useChatStore.setState({ conversationId: "conv_native_commit", abortController: controller });

    const loop = startStreamPump("conv_native_commit", controller, setState, getState);
    await drainAsync();
    expect(sinks).toHaveLength(1);

    sinks[0]!.push(nativeDeltaFrame("m2", 0, "Hello world!"));
    await drainAsync();
    expect(livePreviews().map((b) => b.fullText)).toEqual(["Hello world!"]);

    // The message commits while the socket is dead: the server removes
    // it from reconnect replay (no replay on the next connection) and
    // it lands in the items store instead.
    seedSessionItems("conv_native_commit", [assistantMessage("resp_n", "Hello world!")]);
    sinks[0]!.error();
    await drainAsync();
    expect(sinks).toHaveLength(2);

    // The stale preview is gone — kept, it would double-render beside
    // the committed copy the reconnect backfill splices in.
    expect(livePreviews()).toEqual([]);
    const dones = useChatStore
      .getState()
      .blocks.filter((b): b is Extract<AnyBlock, { type: "text_done" }> => b.type === "text_done");
    // Exactly one rendering of the text: the backfilled committed item.
    // 2 = the pre-fix duplicate (stale preview + committed item).
    expect(dones.map((b) => b.ctx.itemId)).toEqual(["msg_resp_n_asst"]);
    expect(dones[0]!.fullText).toBe("Hello world!");

    const last = sinks[1]!;
    last.push("data: [DONE]\n\n");
    last.close();
    await drainAsync(2);
    await loop;
  });

  it("staggers a background conversation's reconnect but never the visible one", async () => {
    // Every stream is recycled by the ingress at ~5 minutes, so streams opened
    // together drop together. Without a stagger, a tab holding N conversations
    // fires N reconnects — each with a snapshot + items fetch — in one burst.
    seedSession("conv_fg", []);
    seedSession("conv_bg", []);
    const opens: string[] = [];
    const sinks = new Map<string, StreamSink[]>();
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const match = /\/v1\/sessions\/([^/?]+)\/stream/.exec(url);
      if (match) {
        const id = match[1]!;
        opens.push(id);
        const sink = pushableStream();
        sinks.set(id, [...(sinks.get(id) ?? []), sink]);
        return mockResponse(null, { bodyStream: sink.stream });
      }
      return defaultFetchHandler(input, init);
    });

    // conv_bg is bound first, then conv_fg becomes the visible one.
    const bg = bindConversationForTest("conv_bg");
    const bgController = new AbortController();
    const bgLoop = startStreamPump("conv_bg", bgController, bg.set, bg.get);
    await vi.advanceTimersByTimeAsync(1);
    const fg = bindConversationForTest("conv_fg");
    const fgController = new AbortController();
    const fgLoop = startStreamPump("conv_fg", fgController, fg.set, fg.get);
    await vi.advanceTimersByTimeAsync(1);
    expect(opens).toEqual(["conv_bg", "conv_fg"]);

    // Both drop at the same instant, as the ingress deadline would do.
    sinks.get("conv_bg")![0]!.error();
    sinks.get("conv_fg")![0]!.error();
    await vi.advanceTimersByTimeAsync(1);

    // The visible conversation reconnects immediately; the background one waits.
    expect(opens.slice(2)).toEqual(["conv_fg"]);

    // Past the jitter ceiling, the background conversation has reconnected too.
    await vi.advanceTimersByTimeAsync(4000);
    expect(opens.slice(2).sort()).toEqual(["conv_bg", "conv_fg"]);

    releaseConversation("conv_bg");
    releaseConversation("conv_fg");
    bgController.abort();
    fgController.abort();
    for (const list of sinks.values()) {
      for (const sink of list.slice(1)) sink.close();
    }
    await drainAsync(2);
    await Promise.all([bgLoop, fgLoop]);
  });

  it("recomputes the idle flag on every reconnect, so a backgrounded tab reports idle", async () => {
    // The `idle` query param on the stream GET is the whole presence uplink, so
    // the flag has to be recomputed at each (re)connect rather than reused from
    // the first open — that is what keeps presence eventually-correct even when
    // a background tab's debounce timer never fires.
    //
    // This covers the recompute, NOT the abort that triggers it: the mock body
    // stream has no signal wiring, so the drop is simulated explicitly below.
    // `presenceAttemptControllers` (one controller per live stream) is covered
    // directly in "presence idle tracker" instead.
    seedSession("conv_pflip", []);
    const opened: { id: string; idle: boolean }[] = [];
    const sinks: StreamSink[] = [];
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      const match = /\/v1\/sessions\/([^/?]+)\/stream(\?idle=true)?$/.exec(url);
      if (match) {
        opened.push({ id: match[1]!, idle: match[2] !== undefined });
        const sink = pushableStream();
        sinks.push(sink);
        return mockResponse(null, { bodyStream: sink.stream });
      }
      return defaultFetchHandler(input, init);
    });

    const controller = new AbortController();
    useChatStore.setState({ conversationId: "conv_pflip", abortController: controller });
    const loop = startStreamPump("conv_pflip", controller, setState, getState);
    await vi.advanceTimersByTimeAsync(1);
    expect(opened).toEqual([{ id: "conv_pflip", idle: false }]);

    // Tab goes hidden; past the debounce the tracker reports idle by aborting
    // the live attempt.
    Object.defineProperty(document, "hidden", { value: true, configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(PRESENCE_IDLE_AFTER_MS + 1000);

    // A real fetch errors the response body when its signal aborts; the mock
    // stream has no signal wiring, so surface that here. The pump reads the
    // failed read as a drop and reopens carrying the recomputed idle flag.
    sinks[0]!.error(Object.assign(new Error("aborted"), { name: "AbortError" }));
    await vi.advanceTimersByTimeAsync(6000);

    expect(opened.slice(1)).toEqual([{ id: "conv_pflip", idle: true }]);

    Object.defineProperty(document, "hidden", { value: false, configurable: true });
    controller.abort();
    // Only the reopened stream is still open — the first was errored above.
    sinks[sinks.length - 1]!.close();
    await drainAsync(2);
    await loop;
  });
});

// The first-message handoff from the landing composer to ChatPage. The
// read-once delete is what replaces the old router-state clear: it must
// return the prompt exactly once so a refresh/back can't replay it.
describe("pending initial prompt transport", () => {
  it("returns the stashed prompt exactly once, then null", () => {
    setPendingInitialPrompt("conv_abc", { text: "read the README", skill: null });
    // First consume yields the stashed prompt verbatim.
    expect(consumePendingInitialPrompt("conv_abc")).toEqual({
      text: "read the README",
      skill: null,
    });
    // Second consume yields null — the delete prevents a replay.
    expect(consumePendingInitialPrompt("conv_abc")).toBeNull();
  });

  it("returns null for a conversation with no pending prompt", () => {
    expect(consumePendingInitialPrompt("conv_never_set")).toBeNull();
  });

  it("ignores a blank prompt so a blank message never auto-sends", () => {
    setPendingInitialPrompt("conv_blank", { text: "", skill: null });
    // Nothing was stored, so the consume reads null.
    expect(consumePendingInitialPrompt("conv_blank")).toBeNull();
  });

  it("keys prompts by conversation id so they don't cross sessions", () => {
    setPendingInitialPrompt("conv_a", { text: "prompt for A", skill: null });
    setPendingInitialPrompt("conv_b", { text: "prompt for B", skill: null });
    // Each conversation consumes only its own prompt.
    expect(consumePendingInitialPrompt("conv_b")).toEqual({ text: "prompt for B", skill: null });
    expect(consumePendingInitialPrompt("conv_a")).toEqual({ text: "prompt for A", skill: null });
  });

  it("carries a matched skill invocation through intact", () => {
    // The skill payload is what makes ChatPage's auto-send post a
    // slash_command instead of a plain message — if it's dropped or
    // mutated in transit, the first message regresses to literal
    // "/name" text reaching the agent.
    setPendingInitialPrompt("conv_skill", {
      text: "/review-pr 123 focus on auth",
      skill: { name: "review-pr", args: "123 focus on auth" },
    });
    expect(consumePendingInitialPrompt("conv_skill")).toEqual({
      text: "/review-pr 123 focus on auth",
      skill: { name: "review-pr", args: "123 focus on auth" },
    });
  });
});

describe("chatStore — live delta streaming (claude-native)", () => {
  const setState = useChatStore.setState as unknown as Parameters<typeof pumpStreamEvents>[3];
  const getState = useChatStore.getState as unknown as Parameters<typeof pumpStreamEvents>[4];

  /** A FrameScheduler whose pending flush the test fires by hand. */
  function manualScheduler(): { scheduler: FrameScheduler; fire: () => void } {
    let cb: (() => void) | null = null;
    return {
      scheduler: {
        schedule: (c) => {
          cb = c;
        },
        cancel: () => {
          cb = null;
        },
      },
      fire: () => {
        const c = cb;
        cb = null;
        if (c) c();
      },
    };
  }

  /** One `response.output_text.delta` frame carrying native streaming ids. */
  function nativeDelta(messageId: string, index: number, delta: string, final: boolean): string {
    return sse("response.output_text.delta", { delta, message_id: messageId, index, final });
  }

  /** A finalized assistant message `output_item.done` frame. */
  function messageDone(itemId: string, responseId: string, text: string): string {
    return sse("response.output_item.done", {
      item: {
        type: "message",
        role: "assistant",
        id: itemId,
        response_id: responseId,
        content: [{ type: "output_text", text }],
      },
    });
  }

  /** An MCP-shape elicitation/approval request frame. */
  function elicitationReq(id: string): string {
    return sse("response.elicitation_request", {
      elicitation_id: id,
      params: {
        mode: "form",
        message: "approve?",
        phase: "tool_call_approval",
        policy_name: "pol",
        content_preview: "",
      },
    });
  }

  function startPump(conversationId: string): {
    sink: ReturnType<typeof pushableStream>;
    controller: AbortController;
    manual: ReturnType<typeof manualScheduler>;
  } {
    const sink = pushableStream();
    const controller = new AbortController();
    const manual = manualScheduler();
    void pumpStreamEvents(
      conversationId,
      sink.stream,
      controller,
      setState,
      getState,
      manual.scheduler,
    );
    return { sink, controller, manual };
  }

  /** The single provisional `live:*` preview block, or undefined. */
  function provisional(): Extract<AnyBlock, { type: "text_done" }> | undefined {
    return useChatStore
      .getState()
      .blocks.find(
        (b): b is Extract<AnyBlock, { type: "text_done" }> =>
          b.type === "text_done" && b.ctx.itemId?.startsWith("live:") === true,
      );
  }

  it("streams into a provisional block in `blocks`, not a separate lane", async () => {
    useChatStore.setState({
      conversationId: "conv_live",
      blocks: [],
      isNativeTerminalSession: true,
    });
    const { sink, controller } = startPump("conv_live");

    sink.push(sse("response.created", { id: "resp_l", status: "in_progress", output: [] }));
    sink.push(nativeDelta("m1", 0, "Hello ", false));
    sink.push(nativeDelta("m1", 1, "world", true));
    await tick();

    // The streamed text lands as ONE provisional text block in `blocks`
    // (keyed live:m1), accumulating the chunks — so it renders in-order
    // with later committed blocks. If it regressed to a separate lane or
    // the reducer's response-scoped path, this would be absent or a
    // stray text_chunk would appear.
    const prov = provisional();
    expect(prov?.ctx.itemId).toBe("live:m1");
    expect(prov?.fullText).toBe("Hello world");
    expect(useChatStore.getState().blocks.filter((b) => b.type === "text_chunk")).toEqual([]);

    controller.abort();
  });

  it("gives the provisional the LIVE TURN's response id so it shares that bubble", async () => {
    // `walkBubbles` groups by response id, so a synthetic preview id split
    // one native turn into several fragment bubbles while streaming that
    // merged back into one on reload — the turn rendered differently live
    // vs. reloaded, and the shifting fragment boundaries made the "Worked
    // for" fold flicker on codex. The preview must join the live turn.
    useChatStore.setState({
      conversationId: "conv_live_rid",
      blocks: [],
      isNativeTerminalSession: true,
      activeResponse: { responseId: "codex_turn_1", state: "streaming", error: null },
    });
    const { sink, controller } = startPump("conv_live_rid");

    sink.push(nativeDelta("m1", 0, "Checking the CLI", true));
    await tick();

    const prov = provisional();
    expect(prov?.ctx.itemId).toBe("live:m1");
    expect(prov?.ctx.responseId).toBe("codex_turn_1");

    controller.abort();
  });

  it("keeps the synthetic response id when no turn is tracked", async () => {
    // A preview that lands before any turn id is known keeps its own id, so
    // it can't be grouped into an unrelated bubble.
    useChatStore.setState({
      conversationId: "conv_live_norid",
      blocks: [],
      isNativeTerminalSession: true,
      activeResponse: null,
    });
    const { sink, controller } = startPump("conv_live_norid");

    sink.push(nativeDelta("m9", 0, "early chunk", true));
    await tick();

    const prov = provisional();
    expect(prov?.ctx.itemId).toBe("live:m9");
    expect(prov?.ctx.responseId).toBe("live:m9");

    controller.abort();
  });

  it("drops the provisional before processing the authoritative item", async () => {
    useChatStore.setState({
      conversationId: "conv_live2",
      blocks: [],
      isNativeTerminalSession: true,
    });
    const { sink, controller } = startPump("conv_live2");

    sink.push(sse("response.created", { id: "resp_l", status: "in_progress", output: [] }));
    sink.push(nativeDelta("m1", 0, "Hello world", true));
    await tick();
    expect(provisional()?.ctx.itemId).toBe("live:m1");

    sink.push(messageDone("ci_1", "resp_l", "Hello world"));
    await tick();

    // The provisional is gone and the authoritative text_done (real item
    // id) is the only assistant text — no duplicate. If the cleanup
    // regressed, both live:m1 and ci_1 would render.
    expect(provisional()).toBeUndefined();
    const dones = useChatStore
      .getState()
      .blocks.filter((b): b is Extract<AnyBlock, { type: "text_done" }> => b.type === "text_done");
    expect(dones).toHaveLength(1);
    expect(dones[0]!.ctx.itemId).toBe("ci_1");
    expect(dones[0]!.fullText).toBe("Hello world");

    controller.abort();
  });

  it("removes the provisional preview when the authoritative item was already committed", async () => {
    // Race: a snapshot merge (reconnect/rebind) inserts the real
    // assistant item into `blocks` while its `live:*` preview is still
    // on screen. When the authoritative output_item.done then arrives on
    // the stream, the generic item-id dedup skips it — pre-fix, the
    // replacement path never ran and BOTH the real item and the preview
    // stayed rendered (duplicate assistant text, preview at the tail).
    useChatStore.setState({
      conversationId: "conv_live3",
      blocks: [],
      isNativeTerminalSession: true,
    });
    const { sink, controller } = startPump("conv_live3");

    sink.push(sse("response.created", { id: "resp_l", status: "in_progress", output: [] }));
    sink.push(nativeDelta("m1", 0, "Hello world", true));
    await tick();
    expect(provisional()?.ctx.itemId).toBe("live:m1");

    // Snapshot merge lands the committed copy while the preview renders.
    useChatStore.setState((s) => ({
      blocks: [
        ...s.blocks,
        {
          type: "text_done",
          ctx: {
            agent: null,
            depth: 0,
            turn: 0,
            timestamp: 0,
            responseId: "resp_l",
            itemId: "ci_1",
          },
          fullText: "Hello world",
          hasCodeBlocks: false,
        },
      ],
    }));

    sink.push(messageDone("ci_1", "resp_l", "Hello world"));
    await tick();

    // Exactly one rendering: the committed item. The preview is removed.
    expect(provisional()).toBeUndefined();
    const dones = useChatStore
      .getState()
      .blocks.filter((b): b is Extract<AnyBlock, { type: "text_done" }> => b.type === "text_done");
    expect(dones.map((b) => b.ctx.itemId)).toEqual(["ci_1"]);
    expect(dones[0]!.fullText).toBe("Hello world");

    controller.abort();
  });

  it("does not double-render pi-native text when the harness response completes before the deltas", async () => {
    // Faithful replay of a real pi-native turn (captured from a live
    // `omnigent pi` server). The harness PiNativeExecutor completes its
    // Omnigent response the instant it enqueues the user message — so
    // `response.in_progress` + `response.completed` arrive BEFORE Pi's
    // extension streams the assistant text deltas and the authoritative
    // item. The early `response.completed` (response_end) is the missing
    // ingredient the other tests never exercised.
    useChatStore.setState({
      conversationId: "conv_pi",
      blocks: [],
      isNativeTerminalSession: true,
    });
    const { sink, controller } = startPump("conv_pi");

    // Harness turn opens and immediately completes (enqueue → TurnComplete).
    sink.push(
      sse("response.in_progress", {
        id: "resp_harness",
        status: "in_progress",
        model: "pi-native-ui",
        output: [],
      }),
    );
    sink.push(
      sse("response.completed", {
        id: "resp_harness",
        status: "completed",
        model: "pi-native-ui",
        output: [],
      }),
    );
    await tick();

    // Pi then streams the assistant text and posts the authoritative item.
    sink.push(nativeDelta("pi-turn-0:msg:0", 0, "PONG", false));
    sink.push(nativeDelta("pi-turn-0:msg:0", 1, "", true));
    await tick();
    sink.push(messageDone("msg_real", "pi-turn-0", "PONG"));
    await tick();

    // The provisional preview must be removed — exactly one
    // assistant text bubble, not the preview AND the authoritative item.
    expect(provisional()).toBeUndefined();
    const dones = useChatStore
      .getState()
      .blocks.filter((b): b is Extract<AnyBlock, { type: "text_done" }> => b.type === "text_done");
    expect(dones.map((b) => b.ctx.itemId)).toEqual(["msg_real"]);
    expect(dones).toHaveLength(1);

    controller.abort();
  });

  it("keeps an interrupted native partial item marked cancelled", async () => {
    useChatStore.setState({
      conversationId: "conv_interrupt",
      blocks: [],
      isNativeTerminalSession: true,
    });
    const { sink, controller } = startPump("conv_interrupt");

    sink.push(
      sse("session.status", {
        type: "session.status",
        conversation_id: "conv_interrupt",
        status: "running",
        response_id: "codex_turn_123",
      }),
    );
    sink.push(nativeDelta("codex-stream-1", 0, "partial answer", false));
    sink.push(
      sse("session.interrupted", {
        type: "session.interrupted",
        data: { requested_at: 1704067200, response_id: "codex_turn_123" },
      }),
    );
    sink.push(messageDone("ci_partial", "codex_turn_123", "partial answer"));
    sink.push(
      sse("session.status", {
        type: "session.status",
        conversation_id: "conv_interrupt",
        status: "idle",
        response_id: "codex_turn_123",
      }),
    );
    await tick();

    const state = useChatStore.getState();
    const textBlocks = state.blocks.filter(
      (b): b is Extract<AnyBlock, { type: "text_done" }> => b.type === "text_done",
    );
    expect(textBlocks).toHaveLength(1);
    expect(textBlocks[0]!.ctx.itemId).toBe("ci_partial");
    expect(textBlocks[0]!.fullText).toBe("partial answer");
    expect(state.activeResponse).toEqual({
      responseId: "codex_turn_123",
      state: "cancelled",
      error: null,
      completedAt: expect.any(Number),
    });
    expect(state.interruptedResponseIds).toEqual(["codex_turn_123"]);
    useChatStore.setState({ activeResponse: null });
    const settledState = useChatStore.getState();
    const bubbles = buildBubbles(
      settledState.blocks,
      settledState.activeResponse,
      undefined,
      settledState.interruptedResponseIds,
    );
    expect(bubbles).toHaveLength(1);
    expect(bubbles[0]).toMatchObject({
      kind: "assistant",
      responseId: "codex_turn_123",
      lifecycle: "cancelled",
    });

    controller.abort();
  });

  it("removes the preview and handles its done item in normal arrival order", async () => {
    useChatStore.setState({
      conversationId: "conv_live3",
      blocks: [],
      isNativeTerminalSession: true,
    });
    const { sink, controller } = startPump("conv_live3");

    sink.push(sse("response.created", { id: "resp_l", status: "in_progress", output: [] }));
    sink.push(nativeDelta("m1", 0, "Let me check that.", true));
    await tick();
    // Elicitation arrives BEFORE the authoritative text_done (the
    // permission hook fires faster than the transcript poll).
    sink.push(elicitationReq("elicit_1"));
    await tick();

    const idxOf = (pred: (b: AnyBlock) => boolean) =>
      useChatStore.getState().blocks.findIndex(pred);
    const previewIdx = idxOf((b) => b.ctx.itemId === "live:m1");
    const elicitIdx = idxOf((b) => b.type === "elicitation");
    expect(previewIdx).toBeGreaterThanOrEqual(0);
    expect(elicitIdx).toBeGreaterThanOrEqual(0);
    // Streamed text precedes the elicitation in block (== render) order.
    expect(previewIdx).toBeLessThan(elicitIdx);

    // The committed item follows the normal reducer path after the preview
    // is removed, so its position reflects the done event's arrival.
    sink.push(messageDone("ci_1", "resp_l", "Let me check that."));
    await tick();
    const textIdx = idxOf((b) => b.ctx.itemId === "ci_1");
    const elicitIdx2 = idxOf((b) => b.type === "elicitation");
    expect(textIdx).toBeGreaterThanOrEqual(0);
    expect(textIdx).toBeGreaterThan(elicitIdx2);
    expect(provisional()).toBeUndefined();

    controller.abort();
  });

  it("reconciles each preview independently across a multi-message turn", async () => {
    useChatStore.setState({
      conversationId: "conv_live4",
      blocks: [],
      isNativeTerminalSession: true,
    });
    const { sink, controller } = startPump("conv_live4");

    sink.push(sse("response.created", { id: "resp_l", status: "in_progress", output: [] }));
    sink.push(nativeDelta("m1", 0, "first", true));
    await tick();
    sink.push(messageDone("ci_1", "resp_l", "first"));
    await tick();
    // m1 committed, no provisional left.
    expect(provisional()).toBeUndefined();
    // Second message streams after a tool/gap.
    sink.push(nativeDelta("m2", 0, "second", false));
    await tick();

    // m2 is the only in-flight preview (FIFO cleanup took m1, not m2).
    expect(provisional()?.ctx.itemId).toBe("live:m2");
    expect(provisional()?.fullText).toBe("second");
    const dones = useChatStore
      .getState()
      .blocks.filter((b): b is Extract<AnyBlock, { type: "text_done" }> => b.type === "text_done");
    // m1 committed (ci_1) + m2 provisional (live:m2).
    expect(dones.map((b) => b.ctx.itemId)).toEqual(["ci_1", "live:m2"]);

    controller.abort();
  });

  it("drops an unfinalized provisional on response_end (interrupt / drop)", async () => {
    useChatStore.setState({
      conversationId: "conv_live5",
      blocks: [],
      isNativeTerminalSession: true,
    });
    const { sink, controller } = startPump("conv_live5");

    sink.push(sse("response.created", { id: "resp_l", status: "in_progress", output: [] }));
    sink.push(nativeDelta("m1", 0, "partial answer", false));
    await tick();
    expect(provisional()?.ctx.itemId).toBe("live:m1");

    // Turn ends with no committed item for m1 (interrupt before the
    // partial transcript record was forwarded).
    sink.push(sse("response.completed", { id: "resp_l", status: "completed", output: [] }));
    sink.close();
    await tick();
    await tick();

    // The dangling preview is dropped — no forever-streaming bubble.
    expect(provisional()).toBeUndefined();

    controller.abort();
  });
});

describe("chatStore — setCostControlMode", () => {
  it("optimistically flips, PATCHes the wire field, and keeps the server's canonical value", async () => {
    seedSession("conv_cc", []);
    await useChatStore.getState().switchTo("conv_cc");
    // Fresh session: unset → spec default.
    expect(useChatStore.getState().costControlModeOverride).toBeNull();

    const settled = useChatStore.getState().setCostControlMode("on");
    // The flip is visible synchronously, before the PATCH resolves —
    // the optimistic half of the contract (instant pill feedback).
    expect(useChatStore.getState().costControlModeOverride).toBe("on");
    await settled;

    // The PATCH carried the snake_case wire field with the picked value.
    // A missing call (or a camelCase field) means the toggle is local-only
    // and a reload would silently lose the user's choice.
    const patchCall = fetchMock.mock.calls.find(([u, init]) => {
      const url = typeof u === "string" ? u : (u as URL | Request).toString();
      return (
        url === "/v1/sessions/conv_cc" && (init as RequestInit | undefined)?.method === "PATCH"
      );
    });
    expect(patchCall).toBeDefined();
    const body = JSON.parse((patchCall![1] as RequestInit).body as string);
    expect(body).toEqual({ cost_control_mode_override: "on" });
    // Settled state is the server echo, proving the canonical refresh ran.
    expect(useChatStore.getState().costControlModeOverride).toBe("on");
  });

  it("clears the pinned model when routing is turned on (mutual exclusion)", async () => {
    // A session pinned to a model (e.g. Opus from the picker) must drop that
    // pin when routing is enabled — otherwise `model_override` wins and the
    // judge never runs. The clear rides in the SAME PATCH as the toggle.
    seedSession("conv_cc_model", []);
    await useChatStore.getState().switchTo("conv_cc_model");
    useChatStore.setState({ sessionModelOverride: "claude-opus-4-7" });

    // The optimistic clear is visible before the PATCH resolves.
    const settled = useChatStore.getState().setCostControlMode("on");
    expect(useChatStore.getState().sessionModelOverride).toBeNull();
    await settled;

    const patchCall = fetchMock.mock.calls.find(([u, init]) => {
      const url = typeof u === "string" ? u : (u as URL | Request).toString();
      return (
        url === "/v1/sessions/conv_cc_model" &&
        (init as RequestInit | undefined)?.method === "PATCH"
      );
    });
    expect(patchCall).toBeDefined();
    // One PATCH carries BOTH the toggle and the model clear ("default" alias).
    expect(JSON.parse((patchCall![1] as RequestInit).body as string)).toEqual({
      cost_control_mode_override: "on",
      model_override: "default",
    });
  });

  it("does NOT clear the model when routing is turned on but nothing is pinned", async () => {
    // A model-less session (e.g. SDK) must not emit a spurious model clear —
    // that would fire a redundant model_change and a bogus "changed to none".
    seedSession("conv_cc_nomodel", []);
    await useChatStore.getState().switchTo("conv_cc_nomodel");
    expect(useChatStore.getState().sessionModelOverride).toBeNull();

    await useChatStore.getState().setCostControlMode("on");

    const patchCall = fetchMock.mock.calls.find(([u, init]) => {
      const url = typeof u === "string" ? u : (u as URL | Request).toString();
      return (
        url === "/v1/sessions/conv_cc_nomodel" &&
        (init as RequestInit | undefined)?.method === "PATCH"
      );
    });
    expect(patchCall).toBeDefined();
    expect(JSON.parse((patchCall![1] as RequestInit).body as string)).toEqual({
      cost_control_mode_override: "on",
    });
  });

  it("sends an explicit null on clear and settles back to unset", async () => {
    seedSession("conv_cc2", []);
    sessionCostControlOverrides.set("conv_cc2", "off");
    await useChatStore.getState().switchTo("conv_cc2");
    expect(useChatStore.getState().costControlModeOverride).toBe("off");

    await useChatStore.getState().setCostControlMode(null);

    const patches = fetchMock.mock.calls.filter(([u, init]) => {
      const url = typeof u === "string" ? u : (u as URL | Request).toString();
      return (
        url === "/v1/sessions/conv_cc2" && (init as RequestInit | undefined)?.method === "PATCH"
      );
    });
    // Exactly one PATCH, carrying a JSON null — the server's clear signal
    // for this field ("off" is a real value, so no clear alias exists).
    expect(patches).toHaveLength(1);
    expect(JSON.parse((patches[0]![1] as RequestInit).body as string)).toEqual({
      cost_control_mode_override: null,
    });
    expect(useChatStore.getState().costControlModeOverride).toBeNull();
  });

  it("rolls back the optimistic flip when the PATCH fails", async () => {
    seedSession("conv_cc3", []);
    await useChatStore.getState().switchTo("conv_cc3");

    fetchMock.mockImplementationOnce((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/v1/sessions/conv_cc3" && init?.method === "PATCH") {
        return mockResponse({ error: { message: "boom" } }, { ok: false, status: 500 });
      }
      return defaultFetchHandler(input, init);
    });

    await expect(useChatStore.getState().setCostControlMode("on")).rejects.toThrow();
    // Back to the pre-toggle value: the pill must not claim a state the
    // server never persisted (it would silently diverge from the snapshot).
    expect(useChatStore.getState().costControlModeOverride).toBeNull();
  });

  it("rolls back on the toggling conversation when the failure outlives a switch", async () => {
    // The rollback used to be skipped entirely once the user switched away, on
    // the assumption that leaving destroyed the state. It doesn't any more: the
    // conversation stays live, so returning to it showed the optimistic value
    // the server had rejected, permanently.
    seedSession("conv_cc_bg", []);
    seedSession("conv_cc_fg", []);
    await useChatStore.getState().switchTo("conv_cc_bg");

    let failPatch: (() => void) | null = null;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/v1/sessions/conv_cc_bg" && init?.method === "PATCH") {
        return new Promise<Response>((resolve) => {
          failPatch = () =>
            resolve(mockResponse({ error: { message: "boom" } }, { ok: false, status: 500 }));
        });
      }
      return defaultFetchHandler(input, init);
    });

    const toggling = useChatStore.getState().setCostControlMode("on");
    await tick();
    expect(conversationRegistry.peek("conv_cc_bg")!.getState().costControlModeOverride).toBe("on");

    await useChatStore.getState().switchTo("conv_cc_fg");
    failPatch!();
    await expect(toggling).rejects.toThrow();

    // The rejected optimistic flip is gone from the conversation that owns it...
    expect(conversationRegistry.peek("conv_cc_bg")!.getState().costControlModeOverride).toBeNull();
    // ...and the visible conversation was never touched.
    expect(useChatStore.getState().costControlModeOverride).toBeNull();
  });

  it("hydrates from the session snapshot on bind and resets on switch-away", async () => {
    seedSession("conv_cc4", []);
    seedSession("conv_cc5", []);
    sessionCostControlOverrides.set("conv_cc4", "on");

    await useChatStore.getState().switchTo("conv_cc4");
    // Snapshot hydration: the persisted switch lands in store state, so the
    // pill shows the session's real mode on cold load (not the default).
    expect(useChatStore.getState().costControlModeOverride).toBe("on");

    await useChatStore.getState().switchTo("conv_cc5");
    // Session-scoped (NOT a sticky pick): a session without the override
    // reads unset, proving switchTo reset + `?? null` hydration.
    expect(useChatStore.getState().costControlModeOverride).toBeNull();
  });

  it("no-ops without an active conversation", async () => {
    useChatStore.setState({ conversationId: null });
    await useChatStore.getState().setCostControlMode("on");
    // No PATCH was issued — there is no session row to write to.
    const patches = fetchMock.mock.calls.filter(
      ([, init]) => (init as RequestInit | undefined)?.method === "PATCH",
    );
    expect(patches).toHaveLength(0);
  });
});

describe("chatStore — setSubagentRouting", () => {
  /** The session PATCHes issued against `sessionId`, newest last. */
  function patchBodies(sessionId: string): unknown[] {
    return fetchMock.mock.calls
      .filter(([u, init]) => {
        const url = typeof u === "string" ? u : (u as URL | Request).toString();
        return (
          url === `/v1/sessions/${sessionId}` &&
          (init as RequestInit | undefined)?.method === "PATCH"
        );
      })
      .map(([, init]) => JSON.parse((init as RequestInit).body as string));
  }

  it("optimistically writes, PATCHes the wire field, and keeps the server's value", async () => {
    seedSession("conv_sr", []);
    await useChatStore.getState().switchTo("conv_sr");
    // Fresh session: unset, which reads as Default (sub-agents unrouted).
    expect(useChatStore.getState().subagentRoutingOverride).toBeNull();

    const settled = useChatStore.getState().setSubagentRouting("on");
    expect(useChatStore.getState().subagentRoutingOverride).toBe("on");
    await settled;

    expect(patchBodies("conv_sr")).toEqual([{ subagent_routing_override: "on" }]);
    expect(useChatStore.getState().subagentRoutingOverride).toBe("on");
  });

  it("never touches the session's own model or cost switch", async () => {
    // This knob governs the sub-agents' models, so it must not drag the
    // parent's `model_override` / routing switch along the way.
    seedSession("conv_sr2", []);
    await useChatStore.getState().switchTo("conv_sr2");
    useChatStore.setState({ sessionModelOverride: "claude-opus-4-7" });

    await useChatStore.getState().setSubagentRouting("on");

    expect(patchBodies("conv_sr2")).toEqual([{ subagent_routing_override: "on" }]);
    expect(useChatStore.getState().sessionModelOverride).toBe("claude-opus-4-7");
    expect(useChatStore.getState().costControlModeOverride).toBeNull();
  });

  it("rolls back the optimistic write when the PATCH fails", async () => {
    seedSession("conv_sr3", []);
    await useChatStore.getState().switchTo("conv_sr3");

    fetchMock.mockImplementationOnce((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/v1/sessions/conv_sr3" && init?.method === "PATCH") {
        return mockResponse({ error: { message: "boom" } }, { ok: false, status: 500 });
      }
      return defaultFetchHandler(input, init);
    });

    await expect(useChatStore.getState().setSubagentRouting("on")).rejects.toThrow();
    // Back to unset: the row must not claim a state the server never persisted.
    expect(useChatStore.getState().subagentRoutingOverride).toBeNull();
  });

  it("hydrates from the session snapshot on bind and resets on switch-away", async () => {
    seedSession("conv_sr4", []);
    seedSession("conv_sr5", []);
    sessionSubagentRoutingOverrides.set("conv_sr4", "off");

    await useChatStore.getState().switchTo("conv_sr4");
    expect(useChatStore.getState().subagentRoutingOverride).toBe("off");

    await useChatStore.getState().switchTo("conv_sr5");
    // Session-scoped: a session with no override reads back as unset.
    expect(useChatStore.getState().subagentRoutingOverride).toBeNull();
  });

  it("no-ops without an active conversation", async () => {
    useChatStore.setState({ conversationId: null });
    await useChatStore.getState().setSubagentRouting("on");
    const patches = fetchMock.mock.calls.filter(
      ([, init]) => (init as RequestInit | undefined)?.method === "PATCH",
    );
    expect(patches).toHaveLength(0);
  });
});

// Nothing pushes a routing-switch change to the client, so a control that only
// ever read the bind-time snapshot would keep showing a stale value (the
// "Default shown while 'on' is stored" mismatch). These pin the re-read.
describe("chatStore — refreshSessionOverrides", () => {
  it("re-reads both routing switches from the server", async () => {
    seedSession("conv_ro", []);
    await useChatStore.getState().switchTo("conv_ro");
    expect(useChatStore.getState().subagentRoutingOverride).toBeNull();
    expect(useChatStore.getState().costControlModeOverride).toBeNull();

    // Changed out of band (another tab / the CLI), so the store is now stale.
    sessionSubagentRoutingOverrides.set("conv_ro", "on");
    sessionCostControlOverrides.set("conv_ro", "on");
    await useChatStore.getState().refreshSessionOverrides();

    expect(useChatStore.getState().subagentRoutingOverride).toBe("on");
    expect(useChatStore.getState().costControlModeOverride).toBe("on");
  });

  it("reads a cleared override back as inherit", async () => {
    seedSession("conv_ro2", []);
    sessionSubagentRoutingOverrides.set("conv_ro2", "off");
    await useChatStore.getState().switchTo("conv_ro2");
    expect(useChatStore.getState().subagentRoutingOverride).toBe("off");

    sessionSubagentRoutingOverrides.delete("conv_ro2");
    await useChatStore.getState().refreshSessionOverrides();

    expect(useChatStore.getState().subagentRoutingOverride).toBeNull();
  });

  it("keeps the current values when the snapshot fetch fails", async () => {
    seedSession("conv_ro3", []);
    sessionSubagentRoutingOverrides.set("conv_ro3", "on");
    await useChatStore.getState().switchTo("conv_ro3");

    fetchMock.mockImplementationOnce(() =>
      mockResponse({ error: { message: "boom" } }, { ok: false, status: 500 }),
    );
    await useChatStore.getState().refreshSessionOverrides();

    // A blip must not read as "no override" — that would silently flip the
    // row to Inherit and let a Save write it back.
    expect(useChatStore.getState().subagentRoutingOverride).toBe("on");
  });

  it("no-ops without an active conversation", async () => {
    useChatStore.setState({ conversationId: null });
    await useChatStore.getState().refreshSessionOverrides();
    expect(useChatStore.getState().subagentRoutingOverride).toBeNull();
  });
});

// Elicitations are keyed by elicitationId, never itemId (they are not
// persisted conversation items), so neither the reconnect item backfill
// nor the itemId dedupe can see them. These tests cover the dedicated
// elicitation recovery paths: cards surviving a transport drop, prompts
// that fired or resolved while the socket was dead, and the server's
// same-id re-publish on every harness re-park (severed long-poll
// retries reuse their id).
describe("chatStore — elicitations across stream drops and re-publishes", () => {
  const setState = useChatStore.setState as unknown as Parameters<typeof startStreamPump>[2];
  const getState = useChatStore.getState as unknown as Parameters<typeof startStreamPump>[3];

  /** Route `/stream` opens to controllable sinks; everything else hits the
   *  default handler. Duplicated from the reconnect-loop describe for
   *  fixture locality. */
  function routeStreamOpens(): StreamSink[] {
    const sinks: StreamSink[] = [];
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (/\/v1\/sessions\/[^/]+\/stream$/.test(url)) {
        const sink = pushableStream();
        sinks.push(sink);
        return mockResponse(null, { bodyStream: sink.stream });
      }
      return defaultFetchHandler(input, init);
    });
    return sinks;
  }

  /** Raw `pending_elicitations` snapshot entry for one parked prompt —
   *  the shape the server's in-memory index serves on GET /v1/sessions/{id}. */
  function pendingElicitationRaw(id: string, message: string): Record<string, unknown> {
    return {
      type: "response.elicitation_request",
      elicitation_id: id,
      params: {
        mode: "form",
        message,
        requestedSchema: null,
        phase: "tool_call_approval",
        policy_name: "test_policy",
        content_preview: "",
      },
    };
  }

  /** The live SSE frame for the same prompt. */
  function elicitationReqFrame(id: string, message: string): string {
    return sse("response.elicitation_request", {
      elicitation_id: id,
      params: {
        mode: "form",
        message,
        phase: "tool_call_approval",
        policy_name: "test_policy",
        content_preview: "",
      },
    });
  }

  function elicitationCards(): ElicitationBlock[] {
    return useChatStore
      .getState()
      .blocks.filter((b): b is ElicitationBlock => b.type === "elicitation");
  }

  /** Drain the pump + reconcile's sequential await chain under fake timers. */
  async function drainAsync(turns = 25): Promise<void> {
    /* oxlint-disable no-await-in-loop */
    for (let i = 0; i < turns; i += 1) {
      await vi.advanceTimersByTimeAsync(1);
    }
    /* oxlint-enable no-await-in-loop */
  }

  /** Open the stream-pump loop for `id` and drain until the first sink exists. */
  async function openLoop(id: string): Promise<{
    sinks: StreamSink[];
    controller: AbortController;
    loop: Promise<void>;
  }> {
    const sinks = routeStreamOpens();
    const controller = new AbortController();
    useChatStore.setState({ conversationId: id, abortController: controller });
    const loop = startStreamPump(id, controller, setState, getState);
    await drainAsync();
    expect(sinks).toHaveLength(1);
    return { sinks, controller, loop };
  }

  /** End the loop cleanly: [DONE] on the newest sink, then await it. */
  async function closeLoop(sinks: StreamSink[], loop: Promise<void>): Promise<void> {
    const last = sinks[sinks.length - 1]!;
    last.push("data: [DONE]\n\n");
    last.close();
    await drainAsync(2);
    await loop;
  }

  /** End the loop via teardown-style abort: the error() unblocks the
   *  pump's parked read so the awaited loop can actually exit. */
  async function abortLoop(
    sinks: StreamSink[],
    controller: AbortController,
    loop: Promise<void>,
  ): Promise<void> {
    controller.abort();
    sinks[sinks.length - 1]!.error();
    await drainAsync(2);
    await loop;
  }

  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("keeps a pending mid-turn ApprovalCard across a transport drop", async () => {
    seedSession("conv_keep", []);
    // The server still has the prompt parked: its snapshot lists it.
    seedPendingElicitations("conv_keep", [pendingElicitationRaw("elic_keep", "Approve run?")]);
    const { sinks, loop } = await openLoop("conv_keep");

    // Mid-turn: the card is an itemId-less block under the active
    // responseId — exactly the shape dropEphemeralInFlightBlocks targets.
    sinks[0]!.push(sse("response.created", { id: "resp_T", status: "in_progress", output: [] }));
    sinks[0]!.push(elicitationReqFrame("elic_keep", "Approve run?"));
    await drainAsync();
    expect(elicitationCards()).toHaveLength(1);

    sinks[0]!.error();
    await drainAsync();
    expect(sinks).toHaveLength(2);

    const cards = elicitationCards();
    // Exactly one pending card. 0 = the reconnect's ephemeral-block drop
    // deleted the card and nothing restored it (the pre-fix bug: the
    // prompt was unanswerable until a page refresh). 2 = the reconcile
    // replay appended the snapshot's copy without deduping by
    // elicitationId against the surviving card.
    expect(cards).toHaveLength(1);
    expect(cards[0]!.status).toBe("pending");
    expect(cards[0]!.message).toBe("Approve run?");

    await closeLoop(sinks, loop);
  });

  it("renders a prompt that fired while the stream was down", async () => {
    seedSession("conv_gapfire", []);
    const { sinks, loop } = await openLoop("conv_gapfire");
    expect(elicitationCards()).toEqual([]);

    // The prompt fires during the gap: its SSE event went into the dead
    // socket, so it exists only in the snapshot's pending list.
    seedPendingElicitations("conv_gapfire", [pendingElicitationRaw("elic_gap", "Approve gap op?")]);
    sinks[0]!.error();
    await drainAsync();
    expect(sinks).toHaveLength(2);

    const cards = elicitationCards();
    // The reconcile must synthesize the card from the snapshot — items
    // can't resupply it (elicitations are never items), and the SSE
    // stream has no elicitation replay. Empty = the pre-fix bug.
    expect(cards).toHaveLength(1);
    expect(cards[0]!.elicitationId).toBe("elic_gap");
    expect(cards[0]!.status).toBe("pending");
    expect(cards[0]!.message).toBe("Approve gap op?");
    // Gap-fired prompts land at the bottom of the chat — the same
    // position the live stream would have given them.
    expect(useChatStore.getState().blocks.at(-1)!.type).toBe("elicitation");

    await closeLoop(sinks, loop);
  });

  it("flips a card resolved during the gap to Resolved elsewhere", async () => {
    seedSession("conv_gapres", []);
    seedPendingElicitations("conv_gapres", [
      pendingElicitationRaw("elic_res", "Approve soon-resolved?"),
    ]);
    const { sinks, loop } = await openLoop("conv_gapres");

    sinks[0]!.push(elicitationReqFrame("elic_res", "Approve soon-resolved?"));
    await drainAsync();
    expect(elicitationCards()[0]!.status).toBe("pending");

    // Answered elsewhere during the gap: the resolved event fired into
    // the dead socket and the snapshot no longer lists the prompt.
    seedPendingElicitations("conv_gapres", []);
    sinks[0]!.error();
    await drainAsync();
    expect(sinks).toHaveLength(2);

    const cards = elicitationCards();
    expect(cards).toHaveLength(1);
    // The reconcile mirrors the missed elicitation_resolved event by
    // flipping the card to "Resolved elsewhere". If this fails with the
    // card left "pending", it stayed a dead prompt whose Approve button
    // would 404 against the already-cleared parked future.
    expect(cards[0]!.status).toBe("responded");
    expect(cards[0]!.response).toEqual({ action: "auto_resolved" });

    await closeLoop(sinks, loop);
  });

  it("does not duplicate a card when the server re-publishes the same id", async () => {
    seedSession("conv_dup", []);
    const { sinks, controller, loop } = await openLoop("conv_dup");

    // A severed harness wait re-parks and re-publishes the SAME id; a
    // connected client receives both copies on one healthy stream.
    sinks[0]!.push(elicitationReqFrame("elic_dup", "Approve once?"));
    await drainAsync();
    sinks[0]!.push(elicitationReqFrame("elic_dup", "Approve once?"));
    await drainAsync();

    const cards = elicitationCards();
    // One card, still answerable. 2 = the pre-fix bug: every ~5min
    // re-park behind a proxy appended another copy of the same prompt.
    expect(cards).toHaveLength(1);
    expect(cards[0]!.status).toBe("pending");
    expect(cards[0]!.message).toBe("Approve once?");

    await abortLoop(sinks, controller, loop);
  });

  it("revives an auto-resolved card when its prompt re-parks with the same id", async () => {
    seedSession("conv_revive", []);
    const { sinks, controller, loop } = await openLoop("conv_revive");

    sinks[0]!.push(elicitationReqFrame("elic_rev", "Approve revived?"));
    await drainAsync();
    // The deferred clear fired before the harness retry re-parked: the
    // card flips to "Resolved elsewhere" without a user verdict.
    sinks[0]!.push(sse("response.elicitation_resolved", { elicitation_id: "elic_rev" }));
    await drainAsync();
    expect(elicitationCards()[0]!.response).toEqual({ action: "auto_resolved" });

    // The retry re-parks and re-publishes: the prompt is waiting again.
    sinks[0]!.push(elicitationReqFrame("elic_rev", "Approve revived?"));
    await drainAsync();

    const cards = elicitationCards();
    // The existing card is revived in place — answerable again, no
    // duplicate. Stuck at auto_resolved = the user can never answer the
    // re-parked prompt; 2 cards = revive appended instead of flipping.
    expect(cards).toHaveLength(1);
    expect(cards[0]!.status).toBe("pending");
    expect(cards[0]!.response).toBeNull();

    await abortLoop(sinks, controller, loop);
  });

  it("does not clobber an in-flight user verdict with a re-publish", async () => {
    seedSession("conv_verdict", []);
    const { sinks, controller, loop } = await openLoop("conv_verdict");

    sinks[0]!.push(elicitationReqFrame("elic_v", "Approve verdict?"));
    await drainAsync();
    await useChatStore.getState().submitApproval("elic_v", "accept");
    expect(elicitationCards()[0]!.response?.action).toBe("accept");

    // A re-publish racing the verdict POST must not re-open the card —
    // the user already answered; submitApproval owns rolling back its
    // own optimistic flip if the POST fails.
    sinks[0]!.push(elicitationReqFrame("elic_v", "Approve verdict?"));
    await drainAsync();

    const cards = elicitationCards();
    expect(cards).toHaveLength(1);
    expect(cards[0]!.status).toBe("responded");
    expect(cards[0]!.response?.action).toBe("accept");

    await abortLoop(sinks, controller, loop);
  });
});

describe("chatStore — policy deny renders once", () => {
  const setState = useChatStore.setState as unknown as Parameters<typeof pumpStreamEvents>[3];
  const getState = useChatStore.getState as unknown as Parameters<typeof pumpStreamEvents>[4];

  function manualSched() {
    let cb: (() => void) | null = null;
    return {
      scheduler: {
        schedule: (c: () => void) => {
          cb = c;
        },
        cancel: () => {
          cb = null;
        },
      } as FrameScheduler,
      fire: () => {
        const c = cb;
        cb = null;
        if (c) c();
      },
    };
  }

  function denyCount(): number {
    return useChatStore
      .getState()
      .blocks.filter(
        (b) => b.type === "text_done" && (b as TextDone).fullText.includes("Denied by policy"),
      ).length;
  }

  async function run(denyData: Record<string, unknown>): Promise<number> {
    useChatStore.setState({ conversationId: "conv_deny", blocks: [] });
    const sink = pushableStream();
    const controller = new AbortController();
    const manual = manualSched();
    void pumpStreamEvents(
      "conv_deny",
      sink.stream,
      controller,
      setState,
      getState,
      manual.scheduler,
    );
    // Message 2 denied — server publishes the sentinel.
    sink.push(sse("response.output_text.delta", denyData));
    await tick();
    manual.fire();
    await tick();
    // Message 3 submitted → next response starts (the switch that doubled it).
    sink.push(sse("response.created", { id: "resp_2", status: "in_progress", output: [] }));
    sink.push(
      sse("response.output_text.delta", {
        delta: "Hello there friend",
        message_id: "m_reply",
        index: 0,
      }),
    );
    await tick();
    manual.fire();
    await tick();
    const n = denyCount();
    controller.abort();
    return n;
  }

  it("renders once when the deny carries a message_id", async () => {
    // A deny stamped with a stable message_id folds into a single
    // live-preview block and survives the next response switch as one bubble
    // (the server stamps one for every session — native and non-native).
    expect(
      await run({ delta: "[Denied by policy: over budget]", message_id: "deny_abc", index: 0 }),
    ).toBe(1);
  });

  it("keeps the deny visible after its terminal response.completed", async () => {
    // Full server sequence for an input-phase deny: the sentinel delta (a
    // `live:` provisional preview), the committed item as
    // `response.output_item.done` (carrying the persisted itemId), then the
    // terminal `response.completed`. The terminal sweeps unfinalized `live:`
    // previews; the commit event upgrades the deny to a durable, itemId-keyed
    // `text_done` block that survives the sweep (and, being itemId-keyed, a
    // reconnect and a refresh too). Without it the deny vanished until a
    // refresh re-hydrated the persisted item.
    const sentinel = "[Denied by policy: over budget]";
    // Non-native session: the committed item reconciles via the itemId-stamp
    // path (not the native-terminal provisional-replace path).
    useChatStore.setState({
      conversationId: "conv_deny2",
      blocks: [],
      isNativeTerminalSession: false,
    });
    const sink = pushableStream();
    const controller = new AbortController();
    const manual = manualSched();
    void pumpStreamEvents(
      "conv_deny2",
      sink.stream,
      controller,
      setState,
      getState,
      manual.scheduler,
    );
    sink.push(
      sse("response.output_text.delta", {
        delta: sentinel,
        message_id: "deny_xyz",
        index: 0,
      }),
    );
    sink.push(
      sse("response.output_item.done", {
        item: {
          id: "msg_deny_1",
          type: "message",
          role: "assistant",
          response_id: "deny_resp_1",
          content: [{ type: "output_text", text: sentinel }],
        },
      }),
    );
    sink.push(
      sse("response.completed", {
        id: "deny_term",
        status: "completed",
        output: [
          {
            type: "message",
            role: "assistant",
            content: [{ type: "output_text", text: sentinel }],
          },
        ],
      }),
    );
    await tick();
    manual.fire();
    await tick();
    const denyBlocks = useChatStore
      .getState()
      .blocks.filter(
        (b) => b.type === "text_done" && (b as TextDone).fullText.includes("Denied by policy"),
      );
    controller.abort();
    // Exactly one durable deny block survives the terminal sweep, keyed by
    // the persisted itemId (not a swept `live:` provisional id).
    expect(denyBlocks).toHaveLength(1);
    expect(denyBlocks[0]!.ctx.itemId).toBe("msg_deny_1");
  });

  it("keeps the deny visible on a native-terminal session", async () => {
    // Same server sequence as the non-native case, but on a native-terminal
    // session the committed `text_done` first removes the `live:` provisional,
    // then follows the normal committed-item path. Both paths must yield
    // exactly one durable, itemId-keyed deny block.
    const sentinel = "[Denied by policy: over budget]";
    useChatStore.setState({
      conversationId: "conv_deny3",
      blocks: [],
      isNativeTerminalSession: true,
    });
    const sink = pushableStream();
    const controller = new AbortController();
    const manual = manualSched();
    void pumpStreamEvents(
      "conv_deny3",
      sink.stream,
      controller,
      setState,
      getState,
      manual.scheduler,
    );
    sink.push(
      sse("response.output_text.delta", {
        delta: sentinel,
        message_id: "deny_native",
        index: 0,
      }),
    );
    sink.push(
      sse("response.output_item.done", {
        item: {
          id: "msg_deny_native",
          type: "message",
          role: "assistant",
          response_id: "deny_resp_native",
          content: [{ type: "output_text", text: sentinel }],
        },
      }),
    );
    sink.push(
      sse("response.completed", {
        id: "deny_term",
        status: "completed",
        output: [
          {
            type: "message",
            role: "assistant",
            content: [{ type: "output_text", text: sentinel }],
          },
        ],
      }),
    );
    await tick();
    manual.fire();
    await tick();
    const denyBlocks = useChatStore
      .getState()
      .blocks.filter(
        (b) => b.type === "text_done" && (b as TextDone).fullText.includes("Denied by policy"),
      );
    controller.abort();
    // The in-place replace leaves exactly one deny block, keyed by the
    // persisted itemId — no swept `live:` provisional, no duplicate.
    expect(denyBlocks).toHaveLength(1);
    expect(denyBlocks[0]!.ctx.itemId).toBe("msg_deny_native");
  });
});

describe("chatStore — client-side message queue", () => {
  it("enqueueMessage holds the message client-side without POSTing while busy", () => {
    const sendSpy = vi.fn().mockResolvedValue(undefined);
    // Busy: the enqueue-time flush must NOT fire, so both messages stay queued.
    useChatStore.setState({
      conversationId: "conv_abc",
      boundAgentId: "agent_xyz",
      status: "streaming",
      sessionStatus: "running",
      send: sendSpy,
    });
    useChatStore.getState().enqueueMessage("first", undefined);
    useChatStore.getState().enqueueMessage("second", undefined);

    const state = useChatStore.getState();
    expect(state.queuedMessages.map((m) => m.text)).toEqual(["first", "second"]);
    expect(state.queuedMessages.every((m) => m.conversationId === "conv_abc")).toBe(true);
    // Nothing is sent to the server while the agent is busy.
    expect(sendSpy).not.toHaveBeenCalled();
  });

  it("enqueueMessage is a no-op with no bound conversation", () => {
    useChatStore.setState({ conversationId: null });
    useChatStore.getState().enqueueMessage("orphan", undefined);
    expect(useChatStore.getState().queuedMessages).toEqual([]);
  });

  it("dequeueMessage removes the message with the given id, keeping order", () => {
    useChatStore.setState({
      conversationId: "conv_abc",
      queuedMessages: [
        { queueId: "q_1", text: "first", conversationId: "conv_abc" },
        { queueId: "q_2", text: "second", conversationId: "conv_abc" },
        { queueId: "q_3", text: "third", conversationId: "conv_abc" },
      ],
    });
    useChatStore.getState().dequeueMessage("q_2");
    expect(useChatStore.getState().queuedMessages.map((m) => m.text)).toEqual(["first", "third"]);
    // Removing a missing id is a no-op.
    useChatStore.getState().dequeueMessage("q_missing");
    expect(useChatStore.getState().queuedMessages.map((m) => m.text)).toEqual(["first", "third"]);
  });

  it("reorderQueuedMessage moves a message before another within its conversation", () => {
    useChatStore.setState({
      conversationId: "conv_abc",
      queuedMessages: [
        { queueId: "q_1", text: "first", conversationId: "conv_abc" },
        { queueId: "q_2", text: "second", conversationId: "conv_abc" },
        { queueId: "q_3", text: "third", conversationId: "conv_abc" },
      ],
    });

    // Move the last message ahead of the first.
    useChatStore.getState().reorderQueuedMessage("q_3", "q_1");
    expect(useChatStore.getState().queuedMessages.map((m) => m.text)).toEqual([
      "third",
      "first",
      "second",
    ]);

    // beforeQueueId=null moves it to the end.
    useChatStore.getState().reorderQueuedMessage("third", "q_missing"); // (no such row)
    useChatStore.getState().reorderQueuedMessage("q_3", null);
    expect(useChatStore.getState().queuedMessages.map((m) => m.text)).toEqual([
      "first",
      "second",
      "third",
    ]);
  });

  it("reorderQueuedMessage no-ops for a missing id or a self move", () => {
    const initial = [
      { queueId: "q_1", text: "first", conversationId: "conv_abc" },
      { queueId: "q_2", text: "second", conversationId: "conv_abc" },
    ];
    useChatStore.setState({ conversationId: "conv_abc", queuedMessages: initial });

    useChatStore.getState().reorderQueuedMessage("q_missing", "q_1");
    useChatStore.getState().reorderQueuedMessage("q_1", "q_1"); // before itself
    // Reference identity preserved — no state churn on a no-op.
    expect(useChatStore.getState().queuedMessages).toBe(initial);
  });

  it("reorderQueuedMessage only touches its own conversation's slots (interleaved queue)", () => {
    // The flat queue interleaves conversations; reordering conv_abc must leave
    // conv_other's entries at their absolute positions.
    useChatStore.setState({
      conversationId: "conv_abc",
      queuedMessages: [
        { queueId: "a1", text: "a-first", conversationId: "conv_abc" },
        { queueId: "o1", text: "other-1", conversationId: "conv_other" },
        { queueId: "a2", text: "a-second", conversationId: "conv_abc" },
        { queueId: "o2", text: "other-2", conversationId: "conv_other" },
        { queueId: "a3", text: "a-third", conversationId: "conv_abc" },
      ],
    });

    // Move a-third to the front of conv_abc's run.
    useChatStore.getState().reorderQueuedMessage("a3", "a1");

    // conv_abc reordered (a3, a1, a2); conv_other's o1/o2 keep their slots
    // (indices 1 and 3), so the flat array interleaves as below.
    expect(useChatStore.getState().queuedMessages.map((m) => m.queueId)).toEqual([
      "a3",
      "o1",
      "a1",
      "o2",
      "a2",
    ]);
  });

  it("reorderQueuedMessage won't move a message across conversations", () => {
    useChatStore.setState({
      conversationId: "conv_abc",
      queuedMessages: [
        { queueId: "a1", text: "a-first", conversationId: "conv_abc" },
        { queueId: "o1", text: "other-1", conversationId: "conv_other" },
      ],
    });

    // Target belongs to a different conversation → no-op.
    useChatStore.getState().reorderQueuedMessage("a1", "o1");
    expect(useChatStore.getState().queuedMessages.map((m) => m.queueId)).toEqual(["a1", "o1"]);
  });

  it("steerMessage sends the chosen message now and removes it from the queue", () => {
    const sendSpy = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({
      conversationId: "conv_abc",
      boundAgentId: "agent_xyz",
      send: sendSpy,
      queuedMessages: [
        { queueId: "q_1", text: "first", conversationId: "conv_abc" },
        // A message queued while bound to a different agent still steers to
        // the agent it was composed for.
        { queueId: "q_2", text: "second", conversationId: "conv_abc", agentId: "agent_two" },
      ],
    });

    // Steer the second (non-head) message: it sends immediately, out of FIFO
    // order, to the agent captured at enqueue time.
    useChatStore.getState().steerMessage("q_2");
    expect(sendSpy).toHaveBeenCalledTimes(1);
    expect(sendSpy.mock.calls[0]!.slice(0, 2)).toEqual(["second", "agent_two"]);
    expect(useChatStore.getState().queuedMessages.map((m) => m.text)).toEqual(["first"]);

    // Steering a missing id is a no-op.
    useChatStore.getState().steerMessage("q_missing");
    expect(sendSpy).toHaveBeenCalledTimes(1);
  });

  it("steerMessage works on a native-terminal session (harness-agnostic)", () => {
    // Steer is offered for native sessions too: the runner delivers the POSTed
    // message via buffer→drain and the native app folds it in. steerMessage
    // itself doesn't branch on the harness — it just sends now.
    const sendSpy = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({
      conversationId: "conv_abc",
      boundAgentId: "agent_xyz",
      isNativeTerminalSession: true,
      send: sendSpy,
      queuedMessages: [{ queueId: "q_1", text: "steer me", conversationId: "conv_abc" }],
    });
    useChatStore.getState().steerMessage("q_1");
    expect(sendSpy).toHaveBeenCalledTimes(1);
    expect(sendSpy.mock.calls[0]!.slice(0, 2)).toEqual(["steer me", "agent_xyz"]);
    expect(useChatStore.getState().queuedMessages).toEqual([]);
  });

  it("maybeFlushQueuedHead flushes the head FIFO, one per idle", async () => {
    // Spy on send so the flush's contract (which head, in what order) is
    // asserted without depending on the full bind→/events network path.
    const sendSpy = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({
      conversationId: "conv_abc",
      boundAgentId: "agent_xyz",
      status: "idle",
      sessionStatus: "idle",
      send: sendSpy,
      queuedMessages: [
        { queueId: "q_1", text: "first", conversationId: "conv_abc" },
        { queueId: "q_2", text: "second", conversationId: "conv_abc" },
      ],
    });

    // Idle + head present → head ("first") is removed and sent; tail remains.
    useChatStore.getState().maybeFlushQueuedHead();
    await tick();
    expect(useChatStore.getState().queuedMessages.map((m) => m.text)).toEqual(["second"]);
    expect(sendSpy).toHaveBeenCalledTimes(1);
    expect(sendSpy.mock.calls[0]!.slice(0, 2)).toEqual(["first", "agent_xyz"]);

    // Next idle → the next message flushes; queue empties.
    useChatStore.getState().maybeFlushQueuedHead();
    await tick();
    expect(useChatStore.getState().queuedMessages).toEqual([]);
    expect(sendSpy).toHaveBeenCalledTimes(2);
    expect(sendSpy.mock.calls[1]!.slice(0, 2)).toEqual(["second", "agent_xyz"]);
  });

  // Regression: a background shell / still-running sub-agent keeps the session
  // in `waiting` after the turn ends, but the server's turn gate is already
  // free. The flush must NOT treat `waiting` as busy — otherwise a queued
  // message stays stuck until full idle even though a new turn could start now.
  it("flushes the head on `waiting` (background work outlives the turn)", async () => {
    const sendSpy = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({
      conversationId: "conv_abc",
      boundAgentId: "agent_xyz",
      status: "idle",
      sessionStatus: "waiting",
      backgroundTaskCount: 1,
      send: sendSpy,
      queuedMessages: [{ queueId: "q_1", text: "first", conversationId: "conv_abc" }],
    });

    useChatStore.getState().maybeFlushQueuedHead();
    await tick();
    expect(sendSpy).toHaveBeenCalledTimes(1);
    expect(sendSpy.mock.calls[0]!.slice(0, 2)).toEqual(["first", "agent_xyz"]);
    expect(useChatStore.getState().queuedMessages).toEqual([]);
  });

  it("does not flush a queue owned by a different conversation", () => {
    const sendSpy = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({
      conversationId: "conv_abc",
      boundAgentId: "agent_xyz",
      status: "idle",
      sessionStatus: "idle",
      send: sendSpy,
      queuedMessages: [{ queueId: "q_1", text: "elsewhere", conversationId: "conv_other" }],
    });
    useChatStore.getState().maybeFlushQueuedHead();
    // The only queued message belongs to conv_other, so nothing flushes here.
    expect(useChatStore.getState().queuedMessages.map((m) => m.text)).toEqual(["elsewhere"]);
    expect(sendSpy).not.toHaveBeenCalled();
  });

  // Regression: the queue is one flat array across conversations. An undrained
  // message from another conversation must NOT block the bound conversation's
  // messages — flush the first message OF THE BOUND CONVERSATION, not the global
  // array head. A head-only guard stranded the local messages forever.
  it("flushes past a foreign head to reach the bound conversation's message", async () => {
    const sendSpy = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({
      conversationId: "conv_abc",
      boundAgentId: "agent_xyz",
      status: "idle",
      sessionStatus: "idle",
      send: sendSpy,
      queuedMessages: [
        // Foreign message sits at index 0 (queued in conv_other, never drained).
        { queueId: "q_1", text: "foreign", conversationId: "conv_other" },
        { queueId: "q_2", text: "mine-1", conversationId: "conv_abc" },
        { queueId: "q_3", text: "mine-2", conversationId: "conv_abc" },
      ],
    });

    useChatStore.getState().maybeFlushQueuedHead();
    await tick();
    // The bound conversation's FIRST message flushes; the foreign head is left
    // untouched, and the bound conversation's FIFO order is preserved.
    expect(sendSpy).toHaveBeenCalledTimes(1);
    expect(sendSpy.mock.calls[0]!.slice(0, 2)).toEqual(["mine-1", "agent_xyz"]);
    expect(useChatStore.getState().queuedMessages.map((m) => m.text)).toEqual([
      "foreign",
      "mine-2",
    ]);
  });

  // Regression: a message flushes to the agent it was COMPOSED for, even if the
  // binding changed (e.g. a /model switch) between enqueue and drain.
  it("flushes to the agent captured at enqueue time, not the current binding", async () => {
    const sendSpy = vi.fn().mockResolvedValue(undefined);
    // Bound to agent_one when queuing.
    useChatStore.setState({
      conversationId: "conv_abc",
      boundAgentId: "agent_one",
      status: "streaming",
      sessionStatus: "running",
      send: sendSpy,
    });
    useChatStore.getState().enqueueMessage("composed for one", undefined);
    expect(useChatStore.getState().queuedMessages[0]!.agentId).toBe("agent_one");

    // Binding changes to agent_two, then the session idles and flushes.
    useChatStore.setState({ boundAgentId: "agent_two", status: "idle", sessionStatus: "idle" });
    useChatStore.getState().maybeFlushQueuedHead();
    await tick();
    expect(sendSpy).toHaveBeenCalledTimes(1);
    expect(sendSpy.mock.calls[0]!.slice(0, 2)).toEqual(["composed for one", "agent_one"]);
  });

  it("clearQueuedMessages drops only the given conversation's messages", () => {
    useChatStore.setState({
      conversationId: "conv_abc",
      queuedMessages: [
        { queueId: "q_1", text: "a1", conversationId: "conv_abc" },
        { queueId: "q_2", text: "b1", conversationId: "conv_other" },
        { queueId: "q_3", text: "a2", conversationId: "conv_abc" },
      ],
    });
    useChatStore.getState().clearQueuedMessages("conv_abc");
    // Only conv_other's message survives.
    expect(useChatStore.getState().queuedMessages.map((m) => m.text)).toEqual(["b1"]);
  });

  it("does not flush while busy (streaming or running)", () => {
    const sendSpy = vi.fn().mockResolvedValue(undefined);
    const base = {
      conversationId: "conv_abc",
      boundAgentId: "agent_xyz",
      send: sendSpy,
      queuedMessages: [{ queueId: "q_1", text: "wait", conversationId: "conv_abc" }],
    };

    // Local send still in flight.
    useChatStore.setState({ ...base, status: "streaming", sessionStatus: "idle" });
    useChatStore.getState().maybeFlushQueuedHead();
    // Server-side turn still running.
    useChatStore.setState({ ...base, status: "idle", sessionStatus: "running" });
    useChatStore.getState().maybeFlushQueuedHead();

    expect(sendSpy).not.toHaveBeenCalled();
    expect(useChatStore.getState().queuedMessages.map((m) => m.text)).toEqual(["wait"]);
  });

  // Regression: a message queued while the agent was ALREADY idle (the send
  // routed to the queue on a stale busy read, but no future idle edge follows)
  // must still flush. Edge-triggering on the idle SSE event stranded it — the
  // bug this level-triggered design fixes.
  it("flushes a message queued while already idle (no future idle edge)", async () => {
    const sendSpy = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({
      conversationId: "conv_abc",
      boundAgentId: "agent_xyz",
      status: "idle",
      sessionStatus: "idle",
      send: sendSpy,
      queuedMessages: [],
    });

    // Enqueue while idle — enqueueMessage triggers a flush itself, so no
    // session_status event is needed to unstick it.
    useChatStore.getState().enqueueMessage("stranded?", undefined);
    await tick();

    expect(useChatStore.getState().queuedMessages).toEqual([]);
    expect(sendSpy).toHaveBeenCalledTimes(1);
    expect(sendSpy.mock.calls[0]!.slice(0, 2)).toEqual(["stranded?", "agent_xyz"]);
  });

  /**
   * Drive a send whose POST never settles, leaving `status` latched to
   * "streaming", then queue a message behind it.
   *
   * @param rowStatus - the session's status in the sidebar cache, i.e. the
   *   server's own view of whether a turn is running.
   * @returns the texts delivered to /events, appended to as the test advances.
   */
  async function wedgeOnHungSend(rowStatus: Conversation["status"]): Promise<string[]> {
    seedConversationsCache([conv("conv_abc", rowStatus)]);
    useChatStore.setState({
      conversationId: "conv_abc",
      boundAgentId: "agent_xyz",
      abortController: new AbortController(),
      status: "idle",
      sessionStatus: "idle",
      queuedMessages: [],
    });

    const delivered: string[] = [];
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/v1/sessions/conv_abc/events" && init?.method === "POST") {
        const text = JSON.parse(init.body as string).data.content[0].text;
        delivered.push(text);
        if (text === "hung") return new Promise<Response>(() => {});
        return mockResponse({ queued: true, item_id: "ci_ok" });
      }
      return defaultFetchHandler(input, init);
    });

    void useChatStore.getState().send("hung", "agent_xyz");
    await vi.advanceTimersByTimeAsync(1);
    expect(delivered).toEqual(["hung"]);
    expect(useChatStore.getState().status).toBe("streaming");

    // enqueueMessage flushes eagerly; the latch must hold it back.
    useChatStore.getState().enqueueMessage("queued", undefined);
    await vi.advanceTimersByTimeAsync(1);
    expect(delivered).toEqual(["hung"]);
    return delivered;
  }

  it("drains the queue once a stranded send latch outlives any plausible POST", async () => {
    // The wedge: postEvent has no timeout, so a POST whose connection dies
    // mid-flight never settles. `status` stays "streaming" forever and that
    // same flag gates the queue — every later message queues with no error and
    // no recovery short of a page reload.
    vi.useFakeTimers();
    try {
      const delivered = await wedgeOnHungSend("idle");

      // Still within the window a real POST could take: the latch is honored.
      await vi.advanceTimersByTimeAsync(120_000);
      useChatStore.getState().maybeFlushQueuedHead();
      await vi.advanceTimersByTimeAsync(1);
      expect(delivered).toEqual(["hung"]);

      // Past it, with the session's own row reading idle, the latch is
      // stranded: it clears and the queue drains.
      await vi.advanceTimersByTimeAsync(60_000);
      useChatStore.getState().maybeFlushQueuedHead();
      await vi.advanceTimersByTimeAsync(1);
      expect(delivered).toEqual(["hung", "queued"]);
      expect(useChatStore.getState().queuedMessages).toEqual([]);
      // The stale latch was cleared — the drain's own send owns this one, and
      // its clock restarts, so it can't inherit the stranded send's staleness.
      expect(useChatStore.getState().status).toBe("streaming");
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the queue parked when the session is genuinely running", async () => {
    // Same elapsed time, different server truth. Overriding the latch here
    // would double-send into a live turn, so only the server's own idle row
    // may override it.
    vi.useFakeTimers();
    try {
      const delivered = await wedgeOnHungSend("running");

      await vi.advanceTimersByTimeAsync(300_000);
      useChatStore.getState().maybeFlushQueuedHead();
      await vi.advanceTimersByTimeAsync(1);
      expect(delivered).toEqual(["hung"]);
      expect(useChatStore.getState().queuedMessages.map((m) => m.text)).toEqual(["queued"]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("recovers each conversation's own stranded send independently", () => {
    // `status` and the latch are per-conversation now, so clearing ONE
    // conversation's stranded latch must not disarm another's. A single shared
    // timestamp let recovering A strand B — B stayed "streaming" but could no
    // longer age out, so its composer and queue wedged until reload.
    seedConversationsCache([conv("conv_a", "idle"), conv("conv_b", "idle")]);
    // A stale timestamp is stranded without advancing timers (well past the
    // 180s SEND_CHAIN_MAX_WAIT_MS window).
    const stale = Date.now() - 10 * 60_000;
    const wedged: Partial<ConversationState> = {
      status: "streaming",
      sessionStatus: "idle",
      boundAgentId: "agent_xyz",
      activeResponse: null,
      sendLatchedAt: stale,
    };
    // Two conversations, each holding its own hung-send latch.
    bindConversationForTest("conv_a", wedged);
    bindConversationForTest("conv_b", wedged);

    // Recover A (active): its stranded latch clears and its status settles.
    bindConversationForTest("conv_a");
    useChatStore.getState().maybeFlushQueuedHead();
    expect(useChatStore.getState().status).toBe("idle");
    expect(useChatStore.getState().sendLatchedAt).toBeNull();

    // B is untouched — its own latch survived A's recovery.
    const b = conversationRegistry.peek("conv_b")!.getState();
    expect(b.status).toBe("streaming");
    expect(b.sendLatchedAt).toBe(stale);

    // And B is still independently recoverable. Pre-fix, A's recovery nulled the
    // shared scalar, so B's latch read as absent and it could never age out.
    bindConversationForTest("conv_b");
    useChatStore.getState().maybeFlushQueuedHead();
    expect(useChatStore.getState().status).toBe("idle");
  });
});

describe("chatStore — background cross-session flush", () => {
  /** /events POSTs the flush fired, as (conversationId, text) pairs. */
  const eventPosts = (): { id: string; text: string }[] =>
    fetchMock.mock.calls
      .filter(
        ([u, init]) =>
          typeof u === "string" &&
          /\/v1\/sessions\/([^/]+)\/events$/.test(u) &&
          (init as RequestInit | undefined)?.method === "POST",
      )
      .map(([u, init]) => {
        const id = /\/v1\/sessions\/([^/]+)\/events$/.exec(u as string)![1]!;
        const body = JSON.parse((init as RequestInit).body as string);
        const text = (body.data?.content ?? []).find(
          (b: { type: string }) => b.type === "input_text",
        )?.text;
        return { id, text };
      });

  it("flushes an idle non-active conversation's head via postEvent", async () => {
    // Viewing conv_active; conv_bg is idle in the sidebar cache with a queue.
    seedConversationsCache([conv("conv_active", "running"), conv("conv_bg", "idle")]);
    useChatStore.setState({
      conversationId: "conv_active",
      queuedMessages: [
        { queueId: "q_1", text: "bg-first", conversationId: "conv_bg" },
        { queueId: "q_2", text: "bg-second", conversationId: "conv_bg" },
      ],
    });

    useChatStore.getState().flushBackgroundQueues();
    await tick();

    // One POST to conv_bg (FIFO head only); its head left the queue, tail stays.
    expect(eventPosts()).toEqual([{ id: "conv_bg", text: "bg-first" }]);
    expect(useChatStore.getState().queuedMessages.map((m) => m.text)).toEqual(["bg-second"]);
  });

  it("does not flush a non-active conversation that is not idle", async () => {
    seedConversationsCache([conv("conv_active", "idle"), conv("conv_bg", "running")]);
    useChatStore.setState({
      conversationId: "conv_active",
      queuedMessages: [{ queueId: "q_1", text: "wait", conversationId: "conv_bg" }],
    });

    useChatStore.getState().flushBackgroundQueues();
    await tick();

    expect(eventPosts()).toEqual([]);
    expect(useChatStore.getState().queuedMessages.map((m) => m.text)).toEqual(["wait"]);
  });

  it("skips the active conversation (owned by the foreground flush)", async () => {
    // conv_active is idle with a queue, but background flush must leave it to
    // maybeFlushQueuedHead — otherwise both paths would race the same message.
    seedConversationsCache([conv("conv_active", "idle")]);
    useChatStore.setState({
      conversationId: "conv_active",
      queuedMessages: [{ queueId: "q_1", text: "mine", conversationId: "conv_active" }],
    });

    useChatStore.getState().flushBackgroundQueues();
    await tick();

    expect(eventPosts()).toEqual([]);
    expect(useChatStore.getState().queuedMessages.map((m) => m.text)).toEqual(["mine"]);
  });

  it("leaves a message queued when its background POST fails", async () => {
    seedConversationsCache([conv("conv_active", "running"), conv("conv_bg", "idle")]);
    useChatStore.setState({
      conversationId: "conv_active",
      queuedMessages: [{ queueId: "q_1", text: "retry-me", conversationId: "conv_bg" }],
    });
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (/\/v1\/sessions\/conv_bg\/events$/.test(url) && init?.method === "POST") {
        return mockResponse({}, { ok: false, status: 500 });
      }
      return defaultFetchHandler(input, init);
    });

    useChatStore.getState().flushBackgroundQueues();
    await tick();
    await tick();

    // POST failed → the message is re-queued for the next trigger to retry.
    expect(useChatStore.getState().queuedMessages.map((m) => m.text)).toEqual(["retry-me"]);
  });

  it("does not re-POST a just-failed conversation within its cooldown", async () => {
    // Guards the retry-storm case: a persistently-failing idle conversation
    // must not be hammered when the queue-change effect re-fires immediately.
    seedConversationsCache([conv("conv_active", "running"), conv("conv_bg", "idle")]);
    useChatStore.setState({
      conversationId: "conv_active",
      queuedMessages: [{ queueId: "q_1", text: "flaky", conversationId: "conv_bg" }],
    });
    let posts = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (/\/v1\/sessions\/conv_bg\/events$/.test(url) && init?.method === "POST") {
        posts += 1;
        return mockResponse({}, { ok: false, status: 503 });
      }
      return defaultFetchHandler(input, init);
    });

    // First flush POSTs once and fails → re-queued + cooldown set.
    useChatStore.getState().flushBackgroundQueues();
    await tick();
    await tick();
    expect(posts).toBe(1);

    // Immediate re-triggers (mirroring the effect firing on every re-queue)
    // must NOT POST again while the conversation is in cooldown.
    useChatStore.getState().flushBackgroundQueues();
    useChatStore.getState().flushBackgroundQueues();
    await tick();
    expect(posts).toBe(1);
    expect(useChatStore.getState().queuedMessages.map((m) => m.text)).toEqual(["flaky"]);
  });

  it("re-queues a failed head ahead of its own successors (FIFO preserved)", async () => {
    // conv_bg has two queued messages; only the head fails. It must land back
    // in front of its successor, not behind it.
    seedConversationsCache([conv("conv_active", "running"), conv("conv_bg", "idle")]);
    useChatStore.setState({
      conversationId: "conv_active",
      queuedMessages: [
        { queueId: "q_1", text: "bg-first", conversationId: "conv_bg" },
        { queueId: "q_2", text: "bg-second", conversationId: "conv_bg" },
      ],
    });
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (/\/v1\/sessions\/conv_bg\/events$/.test(url) && init?.method === "POST") {
        return mockResponse({}, { ok: false, status: 500 });
      }
      return defaultFetchHandler(input, init);
    });

    useChatStore.getState().flushBackgroundQueues();
    await tick();
    await tick();

    expect(useChatStore.getState().queuedMessages.map((m) => m.text)).toEqual([
      "bg-first",
      "bg-second",
    ]);
  });

  it("uploads an attachment then posts an image block referencing its file_id", async () => {
    // Mirrors send()'s two-phase sequence: upload the file → post the message
    // with the server-assigned file_id, one in-flight guard spanning both.
    seedConversationsCache([conv("conv_active", "running"), conv("conv_bg", "idle")]);
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/v1/sessions/conv_bg/resources/files") && init?.method === "POST") {
        return mockResponse({
          id: "file_bg_1",
          name: "shot.png",
          metadata: { filename: "shot.png", bytes: 4, created_at: 0 },
        });
      }
      return defaultFetchHandler(input, init);
    });
    const file = new File(["png!"], "shot.png", { type: "image/png" });
    useChatStore.setState({
      conversationId: "conv_active",
      queuedMessages: [
        { queueId: "q_1", text: "see this", conversationId: "conv_bg", files: [file] },
      ],
    });

    useChatStore.getState().flushBackgroundQueues();
    await tick();
    await tick();

    // The /events POST carries the image block (real id) followed by the text.
    const post = fetchMock.mock.calls.find(
      ([u, init]) =>
        String(u).endsWith("/v1/sessions/conv_bg/events") &&
        (init as RequestInit | undefined)?.method === "POST",
    );
    expect(post).toBeDefined();
    const content = JSON.parse((post![1] as RequestInit).body as string).data.content;
    expect(content).toEqual([
      { type: "input_image", file_id: "file_bg_1", filename: "shot.png" },
      { type: "input_text", text: "see this" },
    ]);
    expect(useChatStore.getState().queuedMessages).toEqual([]);
  });

  it("re-queues the message when the attachment upload fails", async () => {
    // A failure in the upload phase must re-queue + cool down exactly like a
    // failed POST — the guard spans upload and post together.
    seedConversationsCache([conv("conv_active", "running"), conv("conv_bg", "idle")]);
    let events = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/v1/sessions/conv_bg/resources/files") && init?.method === "POST") {
        return mockResponse({}, { ok: false, status: 500 });
      }
      if (/\/v1\/sessions\/conv_bg\/events$/.test(url) && init?.method === "POST") {
        events += 1;
      }
      return defaultFetchHandler(input, init);
    });
    const file = new File(["x"], "a.png", { type: "image/png" });
    useChatStore.setState({
      conversationId: "conv_active",
      queuedMessages: [
        { queueId: "q_1", text: "with-image", conversationId: "conv_bg", files: [file] },
      ],
    });

    useChatStore.getState().flushBackgroundQueues();
    await tick();
    await tick();

    // Upload failed → no message posted, and the item is re-queued to retry.
    expect(events).toBe(0);
    expect(useChatStore.getState().queuedMessages.map((m) => m.text)).toEqual(["with-image"]);
  });

  it("does not re-upload an attachment when a retry follows a post-phase failure", async () => {
    // Upload succeeds but the post fails → re-queued on cooldown. The retry
    // must reuse the already-uploaded file_id, not upload the blob again (which
    // would orphan the first one server-side).
    seedConversationsCache([conv("conv_active", "running"), conv("conv_bg", "idle")]);
    let uploads = 0;
    let failPost = true;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/v1/sessions/conv_bg/resources/files") && init?.method === "POST") {
        uploads += 1;
        return mockResponse({
          id: "file_bg_dedupe",
          name: "shot.png",
          metadata: { filename: "shot.png", bytes: 4, created_at: 0 },
        });
      }
      if (/\/v1\/sessions\/conv_bg\/events$/.test(url) && init?.method === "POST") {
        if (failPost) return mockResponse({}, { ok: false, status: 500 });
      }
      return defaultFetchHandler(input, init);
    });
    const file = new File(["png!"], "shot.png", { type: "image/png" });
    useChatStore.setState({
      conversationId: "conv_active",
      queuedMessages: [
        { queueId: "q_1", text: "see this", conversationId: "conv_bg", files: [file] },
      ],
    });

    // First attempt: upload lands, post fails → re-queued.
    useChatStore.getState().flushBackgroundQueues();
    await tick();
    await tick();
    expect(uploads).toBe(1);
    expect(useChatStore.getState().queuedMessages.map((m) => m.text)).toEqual(["see this"]);

    // Retry (post now succeeds): the upload must NOT run again. Re-init clears
    // the post-failure cooldown (same query client → seeded cache persists)
    // without touching the queue or the File→upload cache.
    failPost = false;
    initChatStore(client);
    useChatStore.getState().flushBackgroundQueues();
    await tick();
    await tick();
    expect(uploads).toBe(1);
    expect(useChatStore.getState().queuedMessages).toEqual([]);
    // The message that finally posted carries the original uploaded id.
    const posted = fetchMock.mock.calls
      .filter(
        ([u, init]) =>
          String(u).endsWith("/v1/sessions/conv_bg/events") &&
          (init as RequestInit | undefined)?.method === "POST",
      )
      .map(([, init]) => JSON.parse((init as RequestInit).body as string).data.content)
      .at(-1);
    expect(posted).toContainEqual({
      type: "input_image",
      file_id: "file_bg_dedupe",
      filename: "shot.png",
    });
  });

  it("serializes a background flush behind an in-flight foreground send to the SAME conversation", async () => {
    // The navigate-away race: a send() for conv_a is still in flight (its
    // /events POST held open) when the user switches away and conv_a's
    // remaining queue hands off to the background flush. Both POST paths take a
    // slot on conv_a's send chain, so the background POST cannot overtake the
    // foreground one — messages reach the runner in submission order.
    seedConversationsCache([conv("conv_a", "idle"), conv("conv_other", "idle")]);
    useChatStore.setState({
      conversationId: "conv_a",
      boundAgentId: "agent_xyz",
      abortController: new AbortController(),
      status: "idle",
      sessionStatus: "idle",
    });

    // Hold conv_a's first POST open and record delivery order.
    const delivered: string[] = [];
    let releaseForeground: () => void = () => {};
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/v1/sessions/conv_a/events" && init?.method === "POST") {
        const body = JSON.parse((init as RequestInit).body as string);
        const text = (body.data?.content ?? []).find(
          (b: { type: string }) => b.type === "input_text",
        )?.text;
        delivered.push(text);
        if (text === "fg-msg") {
          return new Promise<Response>((resolve) => {
            releaseForeground = () => resolve(mockResponse({ queued: true, item_id: "ci_fg" }));
          });
        }
        return mockResponse({ queued: true, item_id: "ci_bg" });
      }
      return defaultFetchHandler(input, init);
    });

    // Foreground send() takes conv_a's first chain slot; its POST is held open.
    const fg = useChatStore.getState().send("fg-msg", "agent_xyz");
    await tick();
    expect(delivered).toEqual(["fg-msg"]);

    // User navigates away; conv_a's queued follow-up now drains via the
    // background path. It must NOT overtake the in-flight foreground POST.
    useChatStore.setState({
      conversationId: "conv_other",
      queuedMessages: [{ queueId: "q_1", text: "bg-msg", conversationId: "conv_a" }],
    });
    useChatStore.getState().flushBackgroundQueues();
    await tick();
    await tick();
    expect(delivered).toEqual(["fg-msg"]);

    // Release the foreground POST → the background POST is free to deliver.
    releaseForeground();
    await fg;
    await tick();
    await tick();
    expect(delivered).toEqual(["fg-msg", "bg-msg"]);
  });

  it("does not block a background flush behind a send to a DIFFERENT conversation", async () => {
    // Ordering is only meaningful within one conversation: the runner appends
    // per session. A stalled send to the conversation being viewed must not
    // delay an unrelated conversation's queued message — that was a
    // head-of-line block from serializing every POST through one global chain.
    seedConversationsCache([conv("conv_active", "idle"), conv("conv_bg", "idle")]);
    useChatStore.setState({
      conversationId: "conv_active",
      boundAgentId: "agent_xyz",
      abortController: new AbortController(),
      status: "idle",
      sessionStatus: "idle",
      queuedMessages: [{ queueId: "q_1", text: "bg-msg", conversationId: "conv_bg" }],
    });

    const delivered: string[] = [];
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/v1/sessions/conv_active/events" && init?.method === "POST") {
        delivered.push("foreground");
        // Never resolves: conv_active's POST stays in flight for the whole test.
        return new Promise<Response>(() => {});
      }
      if (url === "/v1/sessions/conv_bg/events" && init?.method === "POST") {
        delivered.push("background");
        return mockResponse({ queued: true, item_id: "ci_bg" });
      }
      return defaultFetchHandler(input, init);
    });

    void useChatStore.getState().send("fg-msg", "agent_xyz");
    await tick();
    expect(delivered).toEqual(["foreground"]);

    // conv_bg delivers while conv_active's POST is still hanging.
    useChatStore.getState().flushBackgroundQueues();
    await tick();
    await tick();
    expect(delivered).toEqual(["foreground", "background"]);
  });

  it("does not serialize sends across different conversations", async () => {
    // Ordering only means anything WITHIN a conversation. A single global chain
    // let one stalled POST wedge every session in the tab, so a POST held open
    // for conv_a must not delay a send to conv_b.
    seedConversationsCache([conv("conv_a", "idle"), conv("conv_b", "idle")]);
    useChatStore.setState({
      conversationId: "conv_a",
      boundAgentId: "agent_xyz",
      abortController: new AbortController(),
      status: "idle",
      sessionStatus: "idle",
      queuedMessages: [],
    });

    const delivered: string[] = [];
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/v1/sessions/conv_a/events" && init?.method === "POST") {
        delivered.push("conv_a");
        // Never settles — conv_a's chain stays occupied for the whole test.
        return new Promise<Response>(() => {});
      }
      if (url === "/v1/sessions/conv_b/events" && init?.method === "POST") {
        delivered.push("conv_b");
        return mockResponse({ queued: true, item_id: "ci_b" });
      }
      return defaultFetchHandler(input, init);
    });

    void useChatStore.getState().send("a-msg", "agent_xyz");
    await tick();
    expect(delivered).toEqual(["conv_a"]);

    useChatStore.setState({ conversationId: "conv_b" });
    void useChatStore.getState().send("b-msg", "agent_xyz");
    await tick();
    await tick();
    expect(delivered).toEqual(["conv_a", "conv_b"]);
  });

  it("releases the chain when a prior send's POST never settles", async () => {
    // postEvent issues its fetch with no timeout, so a connection that dies
    // mid-flight never settles and that send's `finally` never runs. The wait on
    // the prior send is bounded so the next send to the SAME conversation still
    // goes out — otherwise the composer queues forever with no error and no
    // recovery short of a page reload.
    vi.useFakeTimers();
    try {
      seedConversationsCache([conv("conv_a", "idle")]);
      useChatStore.setState({
        conversationId: "conv_a",
        boundAgentId: "agent_xyz",
        abortController: new AbortController(),
        status: "idle",
        sessionStatus: "idle",
        queuedMessages: [],
      });

      const delivered: string[] = [];
      fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.toString();
        if (url === "/v1/sessions/conv_a/events" && init?.method === "POST") {
          const text = JSON.parse(init.body as string).data.content[0].text;
          delivered.push(text);
          if (text === "hung") return new Promise<Response>(() => {});
          return mockResponse({ queued: true, item_id: "ci_ok" });
        }
        return defaultFetchHandler(input, init);
      });

      void useChatStore.getState().send("hung", "agent_xyz");
      await vi.advanceTimersByTimeAsync(1);
      expect(delivered).toEqual(["hung"]);

      // Second send parks behind the hung one until the bounded wait expires.
      void useChatStore.getState().send("after", "agent_xyz");
      await vi.advanceTimersByTimeAsync(1);
      expect(delivered).toEqual(["hung"]);

      // A legitimately slow POST (runner session-init, sandbox-host wake) must
      // still keep its ordering — the bound sits above those, not under them.
      await vi.advanceTimersByTimeAsync(120_000);
      expect(delivered).toEqual(["hung"]);

      await vi.advanceTimersByTimeAsync(60_000);
      expect(delivered).toEqual(["hung", "after"]);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("chatStore — attributing pre-turn blocks to the turn", () => {
  const block = (itemId: string | null, responseId: string, text: string): AnyBlock => ({
    type: "text_done",
    ctx: { agent: null, depth: 0, turn: 0, timestamp: 0, responseId, itemId },
    fullText: text,
    hasCodeBlocks: false,
  });

  it("adopts the trailing turn-id-less run when the turn is named", () => {
    // Codex opens its reasoning block ~2s before the `running` edge that
    // carries the turn id, so those blocks are stamped with an empty id
    // and would render as a bubble of their own beside the turn.
    useChatStore.setState({
      conversationId: "conv_abc",
      activeResponse: null,
      blocks: [
        block("m_old", "codex_prev", "previous turn answer"),
        block(null, "", "Thinking…"),
        block(null, "", "…still thinking"),
      ],
    });

    handleSessionEvent({
      type: "session_status",
      conversationId: "conv_abc",
      status: "running",
      responseId: "codex_now",
    });

    const rids = useChatStore.getState().blocks.map((b) => b.ctx.responseId);
    // The prior turn is untouched; both pre-turn blocks join the new turn.
    expect(rids).toEqual(["codex_prev", "codex_now", "codex_now"]);
  });

  it("leaves blocks alone when none are unattributed", () => {
    const blocks = [block("m1", "codex_prev", "answer")];
    useChatStore.setState({ conversationId: "conv_abc", activeResponse: null, blocks });

    handleSessionEvent({
      type: "session_status",
      conversationId: "conv_abc",
      status: "running",
      responseId: "codex_now",
    });

    // Same array identity — no needless rebuild (bubble memoization).
    expect(useChatStore.getState().blocks).toBe(blocks);
  });

  it("does not reach past an attributed block into older history", () => {
    useChatStore.setState({
      conversationId: "conv_abc",
      activeResponse: null,
      blocks: [
        block(null, "", "orphan from an older turn"),
        block("m_old", "codex_prev", "previous turn answer"),
        block(null, "", "Thinking…"),
      ],
    });

    handleSessionEvent({
      type: "session_status",
      conversationId: "conv_abc",
      status: "running",
      responseId: "codex_now",
    });

    const rids = useChatStore.getState().blocks.map((b) => b.ctx.responseId);
    expect(rids).toEqual(["", "codex_prev", "codex_now"]);
  });
});

describe("chatStore — origin-wide stream slots", () => {
  it("holds one slot per live stream while within budget", async () => {
    const slots = makeFakeSlotManager({ capacity: 5 });
    setStreamSlotManagerForTest(slots);
    seedSession("conv_a", []);
    seedSession("conv_b", []);

    await useChatStore.getState().switchTo("conv_a");
    await useChatStore.getState().switchTo("conv_b");

    // Both stream, neither over budget: one held slot each.
    expect(slots.heldByTab()).toBe(2);
    expect(useChatStore.getState().streamBudgetExceeded).toBe(false);
    expect(conversationRegistry.has("conv_a")).toBe(true);
    expect(conversationRegistry.has("conv_b")).toBe(true);
  });

  it("reclaims this tab's own LRU background stream when the origin is saturated", async () => {
    // Two total slots. Opening a third conversation can't take a free slot, so
    // the tab evicts its own oldest background stream (conv_a) to make room —
    // no banner, because it stayed within budget by reclaiming its own.
    const slots = makeFakeSlotManager({ capacity: 2 });
    setStreamSlotManagerForTest(slots);
    for (const id of ["conv_a", "conv_b", "conv_c"]) seedSession(id, []);

    await useChatStore.getState().switchTo("conv_a");
    await useChatStore.getState().switchTo("conv_b");
    await useChatStore.getState().switchTo("conv_c");

    expect(conversationRegistry.has("conv_a")).toBe(false); // evicted (LRU)
    expect(conversationRegistry.has("conv_b")).toBe(true);
    expect(conversationRegistry.has("conv_c")).toBe(true);
    expect(slots.heldByTab()).toBe(2); // exactly the budget, no more
    expect(useChatStore.getState().streamBudgetExceeded).toBe(false);
  });

  it("opens the active conversation over budget, with the banner, when a fresh tab finds every slot held by others", async () => {
    // Every slot held by other tabs; this fresh tab has nothing of its own to
    // reclaim. The conversation must still open (you have to be able to use
    // what you're looking at), and the too-many-tabs banner is raised.
    const slots = makeFakeSlotManager({ capacity: 3, externallyHeld: 3 });
    setStreamSlotManagerForTest(slots);
    seedSession("conv_new_tab", []);

    await useChatStore.getState().switchTo("conv_new_tab");

    expect(useChatStore.getState().conversationId).toBe("conv_new_tab");
    expect(useChatStore.getState().abortController).not.toBeNull(); // stream opened
    expect(slots.heldByTab()).toBe(0); // over budget — no slot held
    expect(useChatStore.getState().streamBudgetExceeded).toBe(true);
  });

  it("re-shows the banner on a fresh over-budget episode after the user dismissed it", async () => {
    const slots = makeFakeSlotManager({ capacity: 2, externallyHeld: 2 });
    setStreamSlotManagerForTest(slots);
    for (const id of ["conv_a", "conv_b", "conv_c"]) seedSession(id, []);

    // Fresh tab, every slot held elsewhere → over budget + banner.
    await useChatStore.getState().switchTo("conv_a");
    expect(useChatStore.getState().streamBudgetExceeded).toBe(true);

    useChatStore.getState().dismissStreamBudgetBanner();
    expect(useChatStore.getState().streamBudgetBannerDismissed).toBe(true);

    // A tab closes → a slot frees; the next open takes it and the episode ends.
    slots.setExternallyHeld(1);
    await useChatStore.getState().switchTo("conv_b");
    expect(useChatStore.getState().streamBudgetExceeded).toBe(false);

    // Origin saturates again → a NEW episode re-shows the banner (un-dismissed).
    slots.setExternallyHeld(2);
    await useChatStore.getState().switchTo("conv_c");
    expect(useChatStore.getState().streamBudgetExceeded).toBe(true);
    expect(useChatStore.getState().streamBudgetBannerDismissed).toBe(false);
  });
});
