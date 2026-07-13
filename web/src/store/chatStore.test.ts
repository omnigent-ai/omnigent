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

import { type InfiniteData, QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation, ConversationsPage } from "@/hooks/useConversations";
import type {
  AnyBlock,
  ElicitationBlock,
  ErrorBlock,
  NativeToolBlock,
  TextDone,
  UserMessageBlock,
} from "@/lib/blocks";
import type { ConversationItem } from "@/lib/conversationItems";
import { itemsToBlocks } from "@/lib/itemsToBlocks";
import { buildBubbles } from "@/lib/renderItems";
import { SESSION_HISTORY_PAGE_SIZE } from "@/lib/sessionsApi";
import { getCurrentAuthorId } from "@/lib/identity";
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
  initChatStore,
  pumpStreamEvents,
  setPendingInitialPrompt,
  startStreamPump,
  useChatStore,
  type FrameScheduler,
} from "./chatStore";
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
  const actual = await importOriginal<typeof import("@/lib/identity")>();
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

// A `[System: …]` marker — sent as a user-role message but re-classified by
// the UI as a muted marker, not a real user turn / rail tick.
function systemMarker(responseId: string, inner = "timer t1 fired"): ConversationItem {
  return {
    id: `msg_${responseId}_sys`,
    response_id: responseId,
    type: "message",
    role: "user",
    status: "completed",
    content: [{ type: "input_text", text: `[System: ${inner}]` }],
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

// A stream that delivers no events and closes with the server's `[DONE]`
// sentinel — i.e. a deliberate clean server close, which the reconnect
// loop in `startStreamPump` treats as terminal (no re-subscribe). Without
// the `[DONE]`, a bare close reads as a transport drop and the loop would
// reconnect, spinning these single-shot tests.
function emptyStream(): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      controller.enqueue(enc.encode("data: [DONE]\n\n"));
      controller.close();
    },
  });
}

function mockResponse(
  body: unknown,
  init?: { ok?: boolean; status?: number; bodyStream?: ReadableStream<Uint8Array> | null },
): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: "OK",
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
let sessionPendingElicitations: Map<string, Array<Record<string, unknown>>>;
let sessionPendingInputs: Map<
  string,
  Array<{ pending_id: string; content: unknown[]; created_by?: string }>
>;
// Per-session cost-control switch the snapshot/PATCH handlers serve;
// absent key = unset (the wire field comes back null).
let sessionCostControlOverrides: Map<string, "on" | "off">;
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

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  sessionSnapshots = new Map();
  sessionItems = new Map();
  sessionPendingElicitations = new Map();
  sessionPendingInputs = new Map();
  sessionCostControlOverrides = new Map();
  sessionLabels = new Map();
  initChatStore(client);
  useChatStore.setState({
    conversationId: null,
    blocks: [],
    pendingUserMessages: [],
    queuedMessages: [],
    // Reset the per-conversation stash too, or a stash entry left by one
    // navigation test leaks into the next (the entry survives switchTo by
    // design — that's the whole point — so beforeEach must clear it).
    pendingByConversation: {},
    activeResponse: null,
    status: "idle",
    sessionStatus: "idle",
    isNativeTerminalSession: false,
    loadingConversation: false,
    conversationLoadError: null,
    costControlModeOverride: null,
    codexPlanMode: false,
    abortController: null,
    // Restore the real send action; a prior test may have stubbed it.
    send: realSend,
  });
  fetchMock.mockReset();
  fetchMock.mockImplementation(defaultFetchHandler);
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  useChatStore.getState().abortController?.abort();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  // Reset the stubbed viewer identity to the default (null) so a value set
  // in one test doesn't leak into the next.
  vi.mocked(getCurrentAuthorId).mockReturnValue(null);
});

/** Yield to the microtask queue so background pump kicks off. */
const tick = () => new Promise<void>((r) => setTimeout(r, 0));

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

function seedPendingElicitations(id: string, events: Array<Record<string, unknown>>): void {
  sessionPendingElicitations.set(id, events);
}

function seedPendingInputs(
  id: string,
  inputs: Array<{ pending_id: string; content: unknown[]; created_by?: string }>,
): void {
  sessionPendingInputs.set(id, inputs);
}

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

  it("restores an un-acked optimistic bubble from the per-conversation stash on navigate-back", async () => {
    // The first-message bug: a native web message's optimistic bubble lives
    // only in client memory until the server records it — the transcript
    // round-trip lags (esp. on a cold-starting runner), so for a window
    // BOTH the items and pending_inputs in the snapshot are empty. If the
    // user navigates away in that window, switchTo wipes the bubble; the
    // navigate-back snapshot is still empty, so without the stash the chat
    // shows nothing until the round-trip's session.input.consumed lands.
    seedSession("conv_native", []); // pre-record: nothing server-side yet
    seedSession("conv_other", []);

    // Land on the native session, then model send()'s optimistic push.
    await useChatStore.getState().switchTo("conv_native");
    useChatStore.setState({
      pendingUserMessages: [{ tempId: "pend_1", content: [{ type: "input_text", text: "hello" }] }],
    });

    // Navigate away (stashes conv_native's bubble) and back.
    await useChatStore.getState().switchTo("conv_other");
    await useChatStore.getState().switchTo("conv_native");

    // Restored from the stash despite the empty snapshot. If the stash
    // logic is reverted, switchTo's reset leaves this [] — the exact
    // disappeared-first-message bug.
    expect(useChatStore.getState().pendingUserMessages).toEqual([
      { tempId: "pend_1", content: [{ type: "input_text", text: "hello" }] },
    ]);
  });

  it("drops the stashed bubble on navigate-back when its message persisted while away", async () => {
    // If the round-trip completes while the user is on another session, the
    // navigate-back snapshot already carries the committed item. The
    // restored stash bubble must be deduped against it (by text/endsWith),
    // or the message double-renders (committed item + trailing bubble).
    seedSession("conv_other", []);

    await useChatStore.getState().switchTo("conv_native");
    useChatStore.setState({
      pendingUserMessages: [{ tempId: "pend_1", content: [{ type: "input_text", text: "hello" }] }],
    });
    await useChatStore.getState().switchTo("conv_other");

    // While away, the message round-tripped into durable history.
    seedSession("conv_native", [userMessage("resp1", "hello")]);
    await useChatStore.getState().switchTo("conv_native");

    // Stash bubble deduped away; the committed item is the only copy. If
    // the dedup didn't apply to restored bubbles, pendingUserMessages would
    // still hold the stale entry (a visible duplicate).
    expect(useChatStore.getState().pendingUserMessages).toEqual([]);
    const userBlocks = useChatStore.getState().blocks.filter((b) => b.type === "user_message");
    expect(userBlocks).toHaveLength(1); // committed "hello" renders exactly once
  });

  it("keeps the stashed bubble when its text matches an OLDER message already in history", async () => {
    // Resumed/disconnected-session regression (the offline-host report): the
    // conversation already has a committed "hello". The user sends another
    // "hello" whose optimistic bubble isn't recorded server-side yet (the
    // runner is relaunching, so the POST is still in flight), then navigates
    // away and back. The navigate-back dedup must NOT treat the PRE-EXISTING
    // committed "hello" as the persisted copy of the new bubble — only a copy
    // NEW since the bubble was stashed counts. Without the baseline the bubble
    // vanishes until its own copy finally commits ("disappears then reappears").
    seedSession("conv_native", [userMessage("resp_old", "hello")]);
    seedSession("conv_other", []);

    await useChatStore.getState().switchTo("conv_native");
    // The snapshot's committed "hello" is in blocks now — it forms the dedup
    // baseline captured when we navigate away. Add the new optimistic bubble.
    useChatStore.setState((s) => ({
      pendingUserMessages: [
        ...s.pendingUserMessages,
        { tempId: "pend_1", content: [{ type: "input_text", text: "hello" }] },
      ],
    }));

    // Away and back — the snapshot still has ONLY the older committed "hello".
    await useChatStore.getState().switchTo("conv_other");
    await useChatStore.getState().switchTo("conv_native");

    // Bubble survives: the older committed copy was already in the baseline,
    // so it isn't mistaken for the new message persisting. Revert the
    // baseline logic and this is [] — the disappeared-message bug.
    expect(useChatStore.getState().pendingUserMessages).toEqual([
      { tempId: "pend_1", content: [{ type: "input_text", text: "hello" }] },
    ]);
    const userBlocks = useChatStore.getState().blocks.filter((b) => b.type === "user_message");
    expect(userBlocks).toHaveLength(1); // the old committed "hello", bubble separate
  });

  it("still drops the stashed bubble when a NEW committed copy appears beside an older match", async () => {
    // The baseline must not over-protect: if history already had a "hello"
    // AND the user's new "hello" round-trips into history while away, the
    // snapshot now holds TWO — one more than the baseline — so the stash
    // bubble IS the persisted one and must be deduped away (no duplicate).
    seedSession("conv_native", [userMessage("resp_old", "hello")]);
    seedSession("conv_other", []);

    await useChatStore.getState().switchTo("conv_native");
    useChatStore.setState((s) => ({
      pendingUserMessages: [
        ...s.pendingUserMessages,
        { tempId: "pend_1", content: [{ type: "input_text", text: "hello" }] },
      ],
    }));
    await useChatStore.getState().switchTo("conv_other");

    // While away, the new "hello" committed — history now has two copies.
    seedSession("conv_native", [
      userMessage("resp_old", "hello"),
      userMessage("resp_new", "hello"),
    ]);
    await useChatStore.getState().switchTo("conv_native");

    expect(useChatStore.getState().pendingUserMessages).toEqual([]);
    const userBlocks = useChatStore.getState().blocks.filter((b) => b.type === "user_message");
    expect(userBlocks).toHaveLength(2); // both committed copies, no trailing bubble
  });

  it("replays all pending_inputs on navigate-back, deduping a restored in-flight twin by content", async () => {
    // Navigate-back re-seeds from the snapshot's pending_inputs — the
    // server is the source of truth for every queued message it knows
    // about, the viewer's own and collaborators' alike (no viewer
    // identity needed). A restored in-flight bubble whose record landed
    // server-side while its POST response was still in transit is
    // dropped in favor of its content-identical server twin: the server
    // entry carries the durable pending_id, so the eventual consumed
    // event clears it BY ID. The own message is image-only here because
    // that's the case only content equality can correlate — the
    // text-based dedupe has nothing to match, and keeping both copies
    // would double-render and strand one of them (the stuck-pending-
    // bubble bug).
    seedSession("conv_other", []);

    await useChatStore.getState().switchTo("conv_native");
    useChatStore.setState({
      pendingUserMessages: [
        {
          tempId: "pend_1",
          content: [{ type: "input_image", file_id: "file_xyz", filename: "a.png" }],
        },
      ],
    });
    await useChatStore.getState().switchTo("conv_other");

    // While away (still pre-persist): the server holds the viewer's own
    // entry (recorded by the in-flight POST) and a collaborator's.
    seedSession("conv_native", []);
    seedPendingInputs("conv_native", [
      {
        pending_id: "pending_mine",
        content: [{ type: "input_image", file_id: "file_xyz", filename: "a.png" }],
        created_by: "alice@databricks.com",
      },
      {
        pending_id: "pending_bob",
        content: [{ type: "input_text", text: "from bob" }],
        created_by: "bob@databricks.com",
      },
    ]);
    await useChatStore.getState().switchTo("conv_native");

    const pending = useChatStore.getState().pendingUserMessages;
    // Both server entries, nothing else. If the restored twin weren't
    // deduped, the image bubble would appear twice (length 3); if
    // pending_inputs replay were dropped, "from bob" would be missing.
    expect(pending).toEqual([
      {
        tempId: "pending_mine",
        content: [{ type: "input_image", file_id: "file_xyz", filename: "a.png" }],
        author: "alice@databricks.com",
      },
      {
        tempId: "pending_bob",
        content: [{ type: "input_text", text: "from bob" }],
        author: "bob@databricks.com",
      },
    ]);
  });

  it("drops a settled send's bubble on navigate-back once the server has resolved it (stuck-bubble regression)", async () => {
    // The reported strand, replayed end-to-end through the real client
    // paths:
    //   1. send an image-only message from the composer (real upload +
    //      POST /events; send() promotes the "pending:" placeholder to
    //      the uploaded file id and stamps `posted` when the POST
    //      settles);
    //   2. confirm the live SSE stream is connected and pumping;
    //   3. navigate away — switchTo aborts the SSE transport, so the
    //      session.input.consumed event the transcript round-trip
    //      publishes while away is fired into a dead socket and lost
    //      (the live stream has no replay). The abort IS the missed
    //      event: no bytes can arrive on a severed transport, so the
    //      test deliberately delivers nothing after this point.
    //   4. while away, the round-trip commits the message server-side
    //      and drains its pending_inputs entry;
    //   5. navigate back — the snapshot shows the committed item and
    //      nothing pending.
    // The send settled, so the server snapshot is authoritative and the
    // bubble must NOT come back. Before the fix the stash restored it at
    // step 5 and nothing could ever clear it — image-only content
    // defeats the text dedupe, native sessions skip the idle-clear, and
    // the consumed event was long gone — leaving a permanent pending
    // bubble that "rotated" on every later send.
    const sink = pushableStream();
    let nativeStreamOpens = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      // First bind gets the controllable live stream (so the test can
      // prove it was connected before the drop); the navigate-back
      // rebind gets a plain empty stream.
      if (url === "/v1/sessions/conv_native/stream") {
        nativeStreamOpens += 1;
        return nativeStreamOpens === 1
          ? mockResponse(null, { bodyStream: sink.stream })
          : streamResponse();
      }
      // Real upload target for the image attachment.
      if (url === "/v1/sessions/conv_native/resources/files" && init?.method === "POST") {
        return mockResponse({
          id: "file_real",
          name: "a.png",
          metadata: { filename: "a.png", bytes: 3, created_at: 0 },
        });
      }
      // Native wrapper: the message round-trips through the TUI, not
      // persisted at POST time — the precondition for the bug.
      if (url.split("?")[0] === "/v1/sessions/conv_native" && (init?.method ?? "GET") === "GET") {
        return mockResponse({
          id: "conv_native",
          agent_id: "agent_xyz",
          status: "idle",
          created_at: 0,
          items: sessionSnapshots.get("conv_native") ?? [],
          pending_inputs: sessionPendingInputs.get("conv_native") ?? [],
          labels: { "omnigent.wrapper": "claude-code-native-ui" },
        });
      }
      return defaultFetchHandler(input, init);
    });
    seedSession("conv_native", []);
    seedSession("conv_other", []);

    await useChatStore.getState().switchTo("conv_native");
    expect(useChatStore.getState().isNativeTerminalSession).toBe(true);

    // 1. Real image-only send: upload → POST /events → settle.
    const image = new File([new Uint8Array([137, 80, 78])], "a.png", { type: "image/png" });
    await useChatStore.getState().send("", "agent_xyz", [image]);

    // The bubble carries the REAL uploaded file id (send promoted the
    // "pending:a.png" placeholder) and the settled flag — proving the
    // full upload + POST path ran, not a hand-injected bubble. If
    // file_id were still "pending:a.png", the upload promotion broke;
    // if posted were undefined, the settle stamp broke and navigation
    // would wrongly stash this bubble.
    const sent = useChatStore.getState().pendingUserMessages;
    expect(sent).toHaveLength(1);
    expect(sent[0]!.content).toEqual([
      { type: "input_image", file_id: "file_real", filename: "a.png" },
    ]);
    expect(sent[0]!.posted).toBe(true);

    // 2. Prove the live stream is connected and pumping BEFORE the
    // drop, so the missed event below is a real loss on a real
    // connection, not an artifact of a stream that never attached.
    sink.push(
      sse("session.status", {
        type: "session.status",
        conversation_id: "conv_native",
        status: "running",
      }),
    );
    await tick();
    expect(useChatStore.getState().sessionStatus).toBe("running");

    // 3. Navigate away. The abort severs the transport — the consumed
    // event published during the gap can never reach this client.
    await useChatStore.getState().switchTo("conv_other");

    // 4. While away: the transcript round-trip committed the message
    // (file blocks merged into the durable item) and drained
    // pending_inputs — the snapshot now shows the committed item and
    // nothing pending.
    seedSession("conv_native", [
      {
        id: "msg_img_user",
        response_id: "resp_img",
        type: "message",
        role: "user",
        status: "completed",
        content: [
          { type: "input_image", file_id: "file_real", filename: "a.png" },
          { type: "input_text", text: "[Attached: a.png]" },
        ],
      },
    ]);

    // 5. Navigate back: no pending bubble survives — the committed item
    // is the only copy. If switchTo still stashed settled sends, this
    // would hold the stranded image bubble forever.
    await useChatStore.getState().switchTo("conv_native");
    expect(useChatStore.getState().pendingUserMessages).toEqual([]);
    const userBlocks = useChatStore.getState().blocks.filter((b) => b.type === "user_message");
    expect(userBlocks).toHaveLength(1); // the committed copy renders exactly once

    // Unpark the first bind's pump so it unwinds cleanly.
    sink.close();
  });

  it("first-message regression: an in-flight send survives navigation; a settled one defers to the server", async () => {
    // End-to-end lifecycle of the navigation-survival policy, driving the
    // REAL send() path (not a setState-injected bubble).
    //
    // Phase 1: a brand-new native session's first POST is held
    // open while the host cold-starts the runner — the server hasn't
    // reached pending_inputs.record() yet, so for that whole window the
    // snapshot has neither a committed item NOR a pending_inputs entry
    // (confirmed in the logz trace: items=0, pending_inputs=0). The mock
    // /events POST never resolves to model that. Navigating away and back
    // must restore the bubble from the client stash — the server cannot
    // replay a message it has never been told about.
    //
    // Phase 2 (stuck-bubble fix): once the POST settles, the server owns
    // the message, so navigation defers to the snapshot. Here the
    // snapshot shows nothing pending (the entry was resolved while
    // away), so the bubble must NOT come back — if it did, nothing
    // could ever clear it (the consumed event is gone and native
    // sessions skip the idle-clear): the permanent stuck bubble.
    let releaseEventsPost: (() => void) | null = null;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      // Cold-starting runner: the /events POST is held open until the
      // test releases it. record() only runs server-side after that.
      if (url.match(/\/v1\/sessions\/[^/]+\/events$/) && init?.method === "POST") {
        return new Promise<Response>((resolve) => {
          releaseEventsPost = () => resolve(mockResponse({ queued: true }));
        }) as unknown as Response;
      }
      // Native wrapper: message round-trips through the TUI, not
      // persisted at POST time (the precondition for both phases).
      if (url.split("?")[0] === "/v1/sessions/conv_native" && (init?.method ?? "GET") === "GET") {
        return mockResponse({
          id: "conv_native",
          agent_id: "agent_xyz",
          status: "idle",
          created_at: 0,
          items: sessionSnapshots.get("conv_native") ?? [],
          pending_inputs: sessionPendingInputs.get("conv_native") ?? [],
          labels: { "omnigent.wrapper": "claude-code-native-ui" },
        });
      }
      return defaultFetchHandler(input, init);
    });
    seedSession("conv_native", []);
    seedSession("conv_other", []);

    // Bind the native session, then send the first message through the
    // real path. Not awaited — the POST is deliberately in flight.
    await useChatStore.getState().switchTo("conv_native");
    const sendDone = useChatStore.getState().send("hello", "agent_xyz");
    await tick(); // let send() reach the held-open POST

    expect(useChatStore.getState().isNativeTerminalSession).toBe(true);
    const sent = useChatStore.getState().pendingUserMessages;
    expect(sent).toHaveLength(1); // optimistic bubble is showing
    expect(sent[0]!.content).toEqual([{ type: "input_text", text: "hello" }]);
    const sentTempId = sent[0]!.tempId; // client-only id; the server has no copy yet

    // Phase 1: navigate away (POST still in flight) and back.
    await useChatStore.getState().switchTo("conv_other");
    await useChatStore.getState().switchTo("conv_native");

    // The SAME optimistic bubble is restored from the stash — proving it
    // came from client memory, not a re-fetch (the snapshot is empty). The
    // stable tempId rules out any other source. Without the stash this is
    // [] and the assertion fails: the disappeared-first-message bug.
    const after = useChatStore.getState().pendingUserMessages;
    expect(after).toHaveLength(1);
    expect(after[0]!.tempId).toBe(sentTempId);
    expect(after[0]!.content).toEqual([{ type: "input_text", text: "hello" }]);

    // Phase 2: the POST settles (runner came up, server recorded +
    // forwarded the message). The live bubble keeps rendering, but it is
    // now server-owned for navigation purposes.
    releaseEventsPost!();
    await sendDone;
    expect(useChatStore.getState().pendingUserMessages).toHaveLength(1); // still on screen

    // Navigate away and back again. The snapshot has no pending_inputs
    // entry (resolved while away) and no committed item yet — the
    // server says nothing is pending, and the server wins. If switchTo
    // still stashed settled sends, the bubble would come back here as a
    // permanently-stuck pending message.
    await useChatStore.getState().switchTo("conv_other");
    await useChatStore.getState().switchTo("conv_native");
    expect(useChatStore.getState().pendingUserMessages).toEqual([]);
  });

  it("drops a settled slash-command echo on navigate-back (nothing can ever reconcile it)", async () => {
    // The optimistic /skill echo follows the same navigation-survival
    // policy as a message send: once its POST settles the server has
    // persisted the visible receipt, so navigation defers to the
    // snapshot. The echo is the most strand-prone bubble if wrongly
    // stashed — its receipt is a SlashCommandBlock, not a user message,
    // so the navigate-back text dedupe can never match it, and no
    // session.input.consumed ever fires for a slash command.
    seedSession("conv_native", []);
    seedSession("conv_other", []);

    await useChatStore.getState().switchTo("conv_native");
    await useChatStore.getState().sendSlashCommand("compact", "", "agent_xyz");

    // Echo is showing (with the typed command text) while we wait for
    // the slash_command receipt event.
    const echo = useChatStore.getState().pendingUserMessages;
    expect(echo).toHaveLength(1);
    expect(echo[0]!.content).toEqual([{ type: "input_text", text: "/compact" }]);

    // Navigate away and back: the settled echo is not restored (the
    // persisted receipt in the snapshot is the durable copy). If
    // sendSlashCommand skipped the posted stamp, the stash would
    // resurrect the echo here as a bubble nothing can clear.
    await useChatStore.getState().switchTo("conv_other");
    await useChatStore.getState().switchTo("conv_native");
    expect(useChatStore.getState().pendingUserMessages).toEqual([]);
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
      new Promise<never>((_, reject) =>
        setTimeout(() => reject(new Error("switchTo timed out waiting for stalled stream")), 200),
      ),
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

  it("hydrates only the most recent page and flags that older history remains", async () => {
    // More than two pages so the loaded window is a strict subset.
    const total = SESSION_HISTORY_PAGE_SIZE * 2 + 5;
    const fullItems = Array.from({ length: total }, (_, idx) =>
      userMessage(`resp_${idx.toString().padStart(4, "0")}`, `message ${idx}`),
    );
    seedSessionSnapshot("conv_big", fullItems.slice(-SESSION_HISTORY_PAGE_SIZE));
    seedSessionItems("conv_big", fullItems);

    await useChatStore.getState().switchTo("conv_big");

    const state = useChatStore.getState();
    // Only the newest page is hydrated. If bind regressed to loading the
    // whole transcript this would be `total` (205) — the slow-open bug
    // this windowing fixes.
    expect(state.blocks).toHaveLength(SESSION_HISTORY_PAGE_SIZE);
    // Newest item is the last block: proves we fetched the tail
    // (order=desc) and reversed to chronological, not the convo head.
    expect(state.blocks.at(-1)).toMatchObject({
      type: "user_message",
      ctx: { itemId: `msg_resp_${(total - 1).toString().padStart(4, "0")}_user` },
    });
    // Older items remain, so scroll-up loading is armed. `false` here
    // would strand the user with no way to reach earlier turns.
    expect(state.hasMoreHistory).toBe(true);
    // One descending page request on bind — not the sequential per-page
    // walk the old full-transcript hydration did.
    const itemFetches = fetchMock.mock.calls.filter(([u]) =>
      String(u).startsWith("/v1/sessions/conv_big/items"),
    );
    expect(itemFetches).toHaveLength(1);
    expect(String(itemFetches[0]![0])).toContain("order=desc");
  });

  it("loadMoreHistory prepends the page of items immediately older than the window", async () => {
    const total = SESSION_HISTORY_PAGE_SIZE * 2 + 5;
    const fullItems = Array.from({ length: total }, (_, idx) =>
      userMessage(`resp_${idx.toString().padStart(4, "0")}`, `message ${idx}`),
    );
    seedSessionSnapshot("conv_big", fullItems.slice(-SESSION_HISTORY_PAGE_SIZE));
    seedSessionItems("conv_big", fullItems);

    await useChatStore.getState().switchTo("conv_big");
    // Precondition: bind loaded exactly the newest page.
    expect(useChatStore.getState().blocks).toHaveLength(SESSION_HISTORY_PAGE_SIZE);

    await useChatStore.getState().loadMoreHistory();

    const state = useChatStore.getState();
    // Two pages now loaded (initial + one older page). A wrong older-page
    // cursor — e.g. the pre-fix bug that fetched the convo head — would
    // yield the wrong count or duplicate blocks.
    expect(state.blocks).toHaveLength(SESSION_HISTORY_PAGE_SIZE * 2);
    // The older page sits before the original, oldest item first. Its
    // first block is index `total - 2*PAGE` (= 5): proves the prepend
    // order and the descending+after cursor math are correct.
    expect(state.blocks[0]).toMatchObject({
      type: "user_message",
      ctx: {
        itemId: `msg_resp_${(total - 2 * SESSION_HISTORY_PAGE_SIZE)
          .toString()
          .padStart(4, "0")}_user`,
      },
    });
    // 5 still-older items (total - 2*PAGE) remain, so more is flagged.
    expect(state.hasMoreHistory).toBe(true);
  });

  it("drops a stale loadMoreHistory page that resolves after navigating away and back", async () => {
    const total = SESSION_HISTORY_PAGE_SIZE * 2;
    const itemsA = Array.from({ length: total }, (_, idx) =>
      userMessage(`a_${idx.toString().padStart(4, "0")}`, `a ${idx}`),
    );
    seedSession("conv_a", itemsA);
    seedSession("conv_b", []);

    await useChatStore.getState().switchTo("conv_a");
    const windowIds = useChatStore.getState().blocks.map((b) => b.ctx.itemId);
    expect(windowIds).toHaveLength(SESSION_HISTORY_PAGE_SIZE);

    // Defer ONLY the first scroll-up page (the `after=` cursor fetch);
    // binds use cursorless initial-window fetches and stay live.
    let releaseStalePage: (() => void) | null = null;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (/\/v1\/sessions\/conv_a\/items\?.*after=/.test(url) && releaseStalePage === null) {
        return new Promise<Response>((resolve) => {
          releaseStalePage = () => resolve(defaultFetchHandler(input, init));
        });
      }
      return defaultFetchHandler(input, init);
    });

    const stale = useChatStore.getState().loadMoreHistory();
    await useChatStore.getState().switchTo("conv_b");
    await useChatStore.getState().switchTo("conv_a"); // fresh window for A
    releaseStalePage!();
    await stale;

    const state = useChatStore.getState();
    // The stale page is cursor-relative to the PRE-navigation window; the
    // round trip passed the conversation-id guard, so only the generation
    // check stops it from prepending below the fresh hydration merge.
    expect(state.blocks.map((b) => b.ctx.itemId)).toEqual(windowIds);
    expect(state.oldestItemId).toBe(itemsA.at(-SESSION_HISTORY_PAGE_SIZE)!.id);
    expect(state.hasMoreHistory).toBe(true);
    expect(state.loadingMoreHistory).toBe(false);

    // The window is healthy: a fresh scroll-up still pages correctly.
    await useChatStore.getState().loadMoreHistory();
    expect(useChatStore.getState().blocks.map((b) => b.ctx.itemId)).toEqual([
      ...itemsA.slice(0, SESSION_HISTORY_PAGE_SIZE).map((it) => it.id),
      ...windowIds,
    ]);
  });

  it("loadMoreHistory dedupes a page overlapping blocks kept across a rebind", async () => {
    const total = SESSION_HISTORY_PAGE_SIZE * 3;
    const items = Array.from({ length: total }, (_, idx) =>
      userMessage(`r_${idx.toString().padStart(4, "0")}`, `r ${idx}`),
    );
    seedSession("conv_re", items);

    await useChatStore.getState().switchTo("conv_re");
    await useChatStore.getState().loadMoreHistory();
    expect(useChatStore.getState().blocks).toHaveLength(2 * SESSION_HISTORY_PAGE_SIZE);

    // The stream died (idle disconnect): the send-triggered rebind
    // re-hydrates the newest window, resetting the cursor to its top while
    // the scrolled-up blocks stay rendered.
    useChatStore.setState({ abortController: null });
    await useChatStore.getState().send("hello again", "agent_xyz");
    expect(useChatStore.getState().oldestItemId).toBe(items.at(-SESSION_HISTORY_PAGE_SIZE)!.id);

    // This page (older than the rewound cursor) fully overlaps the blocks
    // kept across the rebind — without itemId dedupe each would render twice.
    await useChatStore.getState().loadMoreHistory();
    expect(useChatStore.getState().blocks.map((b) => b.ctx.itemId)).toEqual(
      items.slice(SESSION_HISTORY_PAGE_SIZE).map((it) => it.id),
    );

    // The cursor advanced through the overlap, so the oldest page loads next.
    await useChatStore.getState().loadMoreHistory();
    expect(useChatStore.getState().blocks.map((b) => b.ctx.itemId)).toEqual(
      items.map((it) => it.id),
    );
  });

  it("clears loadingMoreHistory when a rebind hydration voids the in-flight page", async () => {
    const total = SESSION_HISTORY_PAGE_SIZE * 2;
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
      ...items.slice(0, SESSION_HISTORY_PAGE_SIZE).map((it) => it.id),
      ...windowIds,
    ]);
  });

  it("loadHistoryUntilUserMessages pages older history until the target user count", async () => {
    // Dense turns: each turn is one user message followed by several
    // non-user items (assistant + tool call), so a page spans many items per
    // user prompt — the case the eager loader's large page size targets.
    // 15 turns × 3 items = 45 items, comfortably past the initial window so
    // the eager loader genuinely pages older history rather than no-opping.
    const TURNS = 15;
    const items: ConversationItem[] = [];
    for (let t = 0; t < TURNS; t++) {
      const rid = `t_${t.toString().padStart(3, "0")}`;
      items.push(userMessage(rid, `prompt ${t}`));
      items.push(nativeToolItem(rid));
      items.push(assistantMessage(rid, `reply ${t}`));
    }
    seedSessionSnapshot("conv_dense", items.slice(-SESSION_HISTORY_PAGE_SIZE));
    seedSessionItems("conv_dense", items);

    await useChatStore.getState().switchTo("conv_dense");
    // The initial window holds only the newest slice of items, so far fewer
    // than TURNS user prompts are loaded — the stub the eager loader fills in.
    const initialUsers = useChatStore
      .getState()
      .blocks.filter((b) => b.type === "user_message").length;
    expect(initialUsers).toBeLessThan(TURNS);

    await useChatStore.getState().loadHistoryUntilUserMessages(TURNS);

    const state = useChatStore.getState();
    // Reached (at least) the requested user count and ran history dry.
    expect(state.blocks.filter((b) => b.type === "user_message")).toHaveLength(TURNS);
    expect(state.hasMoreHistory).toBe(false);
    expect(state.loadingMoreHistory).toBe(false);
  });

  it("loadHistoryUntilUserMessages keeps items in chronological order", async () => {
    // Regression: the eager loader used to prepend each fetched block
    // one-by-one, which reversed every page's internal order and scrambled
    // the transcript (a mid-conversation prompt would surface at the top).
    // Dense turns force multi-item pages so any per-page reversal shows up.
    // 15 turns × 3 items is well past the initial window, so the eager loader
    // pages a real multi-item older window (where the reversal manifested).
    const TURNS = 15;
    const items: ConversationItem[] = [];
    for (let t = 0; t < TURNS; t++) {
      const rid = `o_${t.toString().padStart(3, "0")}`;
      items.push(userMessage(rid, `prompt ${t}`));
      items.push(nativeToolItem(rid));
      items.push(assistantMessage(rid, `reply ${t}`));
    }
    seedSessionSnapshot("conv_order", items.slice(-SESSION_HISTORY_PAGE_SIZE));
    seedSessionItems("conv_order", items);

    await useChatStore.getState().switchTo("conv_order");
    await useChatStore.getState().loadHistoryUntilUserMessages(TURNS);

    // The loaded blocks must match the seeded chronological order exactly.
    const loadedIds = useChatStore
      .getState()
      .blocks.map((b) => b.ctx.itemId)
      .filter((iid): iid is string => Boolean(iid));
    expect(loadedIds).toEqual(items.map((item) => item.id));
    // And user prompts read oldest-first (prompt 0 before prompt 1 …).
    const userIds = useChatStore
      .getState()
      .blocks.filter((b): b is UserMessageBlock => b.type === "user_message")
      .map((b) => b.ctx.itemId);
    const seededUserIds = items
      .filter((item) => item.type === "message" && item.role === "user")
      .map((item) => item.id);
    expect(userIds).toEqual(seededUserIds);
  });

  it("loadHistoryUntilUserMessages no-ops when the target is already met", async () => {
    const items = Array.from({ length: SESSION_HISTORY_PAGE_SIZE }, (_, idx) =>
      userMessage(`m_${idx.toString().padStart(3, "0")}`, `m ${idx}`),
    );
    seedSession("conv_met", items);

    await useChatStore.getState().switchTo("conv_met");
    const before = useChatStore.getState().blocks.map((b) => b.ctx.itemId);
    const itemFetchesBefore = fetchMock.mock.calls.filter(([u]) =>
      String(u).startsWith("/v1/sessions/conv_met/items"),
    ).length;

    // The window already holds SESSION_HISTORY_PAGE_SIZE user prompts, so a
    // smaller target must not fetch or mutate anything.
    await useChatStore.getState().loadHistoryUntilUserMessages(2);

    expect(useChatStore.getState().blocks.map((b) => b.ctx.itemId)).toEqual(before);
    const itemFetchesAfter = fetchMock.mock.calls.filter(([u]) =>
      String(u).startsWith("/v1/sessions/conv_met/items"),
    ).length;
    expect(itemFetchesAfter).toBe(itemFetchesBefore);
  });

  it("loadHistoryUntilUserMessages disables further history on fetch failure", async () => {
    // The rail's eager-load effect auto-fires this with no user gesture, so a
    // persistent fetch failure that left hasMoreHistory true would re-arm the
    // effect and hammer the failing endpoint in a tight loop. On error we
    // commit progress AND clear hasMoreHistory to break that loop.
    const TURNS = 15;
    const items: ConversationItem[] = [];
    for (let t = 0; t < TURNS; t++) {
      const rid = `f_${t.toString().padStart(3, "0")}`;
      items.push(userMessage(rid, `prompt ${t}`));
      items.push(assistantMessage(rid, `reply ${t}`));
    }
    seedSessionSnapshot("conv_fail", items.slice(-SESSION_HISTORY_PAGE_SIZE));
    seedSessionItems("conv_fail", items);

    await useChatStore.getState().switchTo("conv_fail");
    expect(useChatStore.getState().hasMoreHistory).toBe(true);

    // Fail every older-history page fetch (the `olderThan=` cursor read).
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (/\/v1\/sessions\/conv_fail\/items\?.*olderThan=/.test(url)) {
        return Promise.reject(new Error("boom"));
      }
      return defaultFetchHandler(input, init);
    });

    await useChatStore.getState().loadHistoryUntilUserMessages(TURNS);

    const state = useChatStore.getState();
    // hasMoreHistory is cleared so the auto-firing effect can't re-loop.
    expect(state.hasMoreHistory).toBe(false);
    expect(state.loadingMoreHistory).toBe(false);
  });

  it("loadHistoryUntilUserMessages ignores [System: …] markers when counting turns", async () => {
    // The rail derives ticks from REAL user turns only (system markers are
    // dropped), so the loader must count the same way. If it counted markers,
    // it could early-return at the target while the rail has too few ticks —
    // and, since the early-return leaves hasMoreHistory set, wedge the rail
    // permanently hidden. Here every turn carries a [System: …] marker: with
    // 15 real turns the loader must page ALL of them (running history dry)
    // rather than stopping early on the inflated user-block count.
    const TARGET = 10;
    const TURNS = 15;
    const items: ConversationItem[] = [];
    for (let t = 0; t < TURNS; t++) {
      const rid = `s_${t.toString().padStart(3, "0")}`;
      items.push(userMessage(rid, `prompt ${t}`));
      items.push(systemMarker(rid)); // a user-role block that is NOT a real turn
      items.push(assistantMessage(rid, `reply ${t}`));
    }
    seedSessionSnapshot("conv_sys", items.slice(-SESSION_HISTORY_PAGE_SIZE));
    seedSessionItems("conv_sys", items);

    await useChatStore.getState().switchTo("conv_sys");
    await useChatStore.getState().loadHistoryUntilUserMessages(TARGET);

    const state = useChatStore.getState();
    // Real user turns actually loaded — the count that feeds the rail.
    const blockText = (b: (typeof state.blocks)[number]): string =>
      b.type === "user_message"
        ? b.content
            .filter((c): c is { type: "input_text"; text: string } => c.type === "input_text")
            .map((c) => c.text)
            .join("")
        : "";
    const realTurns = state.blocks.filter(
      (b) => b.type === "user_message" && !/^\[System: /.test(blockText(b)),
    ).length;
    expect(realTurns).toBeGreaterThanOrEqual(TARGET);
    // hasMoreHistory settles false only because we counted real turns, not the
    // (larger) raw user-block count — proving the loader didn't stop early.
    expect(state.loadingMoreHistory).toBe(false);
  });

  it("loadHistoryUntilUserMessages assembles turns across multiple pages in order", async () => {
    // EAGER_PAGE_LIMIT is 200, so to actually exercise the cross-page
    // `older.unshift(...)` assembly (and the MAX_EAGER_PAGES loop) we need more
    // than 200 items. 120 turns × 2 items = 240 items > one page.
    const TURNS = 120;
    const items: ConversationItem[] = [];
    for (let t = 0; t < TURNS; t++) {
      const rid = `p_${t.toString().padStart(3, "0")}`;
      items.push(userMessage(rid, `prompt ${t}`));
      items.push(assistantMessage(rid, `reply ${t}`));
    }
    seedSessionSnapshot("conv_multi", items.slice(-SESSION_HISTORY_PAGE_SIZE));
    seedSessionItems("conv_multi", items);

    await useChatStore.getState().switchTo("conv_multi");
    // Target more turns than fit in a single 200-item page, forcing ≥2 pages.
    await useChatStore.getState().loadHistoryUntilUserMessages(110);

    // Whatever loaded must be a contiguous chronological suffix of the seed —
    // any per-page reversal or misordered cross-page splice would break this.
    const loadedIds = useChatStore
      .getState()
      .blocks.map((b) => b.ctx.itemId)
      .filter((iid): iid is string => Boolean(iid));
    const seededIds = items.map((it) => it.id);
    const suffix = seededIds.slice(seededIds.length - loadedIds.length);
    expect(loadedIds).toEqual(suffix);
    expect(loadedIds.length).toBeGreaterThan(2 * SESSION_HISTORY_PAGE_SIZE);
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

  it("aborts the in-flight stream's controller when switching conversations", async () => {
    const controller = new AbortController();
    useChatStore.setState({
      conversationId: "conv_abc",
      abortController: controller,
    });
    seedSession("conv_def");

    await useChatStore.getState().switchTo("conv_def");

    expect(controller.signal.aborted).toBe(true);
    // The NEW bind creates a fresh controller; that one stays for the
    // lifetime of the new session's stream.
    expect(useChatStore.getState().conversationId).toBe("conv_def");
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
    const resolvers: Array<() => void> = [];
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
    // Denying the queued input must not mark the already-streaming prior
    // response idle; that response still needs its own response_end.
    expect(state.status).toBe("streaming");
    expect(state.sessionStatus).toBe("running");
    expect(state.activeResponse).toEqual({
      responseId: "resp_in_flight",
      state: "streaming",
      error: null,
    });

    handleSessionEvent({
      type: "session_status",
      conversationId: "conv_abc",
      status: "idle",
    });
    const afterIdle = useChatStore.getState();
    // The server's deny short-circuit publishes running→idle for the
    // queued input. That idle must not clear the prior response's
    // running/working signal while its active response is still streaming.
    expect(afterIdle.status).toBe("streaming");
    expect(afterIdle.sessionStatus).toBe("running");
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

  it("clears the shell count when a new turn starts (running edge)", () => {
    // A `running` edge with no count means a fresh turn began; the prior
    // turn's tally is stale and must clear, mirroring the server's
    // `_publish_status`. (The PTY `running` carries no count.)
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
    expect(useChatStore.getState().backgroundTaskCount).toBe(0);
  });

  it("clears the sticky shell count when the user sends a new turn", async () => {
    // Asking another question while shells run supersedes the prior turn's
    // tally: the label must flip from "N background tasks still running" to "Working…"
    // immediately, not linger until the next status edge.
    seedSession("conv_abc");
    useChatStore.setState({
      conversationId: "conv_abc",
      sessionStatus: "waiting",
      backgroundTaskCount: 2,
      status: "idle",
      activeResponse: null,
    });
    await useChatStore.getState().send("another question", "agent_xyz");
    expect(useChatStore.getState().backgroundTaskCount).toBe(0);
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
        pendingByConversation: {
          conv_old: {
            messages: [{ tempId: "pend_clear", content: [{ type: "input_text", text: "/clear" }] }],
            committedTexts: [],
          },
        },
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
      // spinning forever — both the live list and the navigate-back stash.
      expect(state.pendingUserMessages).toEqual([]);
      expect(state.pendingByConversation.conv_old).toBeUndefined();
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

    it("idle preserves a still-streaming active response until response_end arrives", () => {
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
      // A real response bubble is closed by response_end, not by the
      // coarser session.status signal. Clearing status here would make
      // the bubble lifecycle disagree with the streaming reducer.
      expect(state.status).toBe("streaming");
      expect(state.activeResponse).toEqual({
        responseId: "resp_live",
        state: "streaming",
        error: null,
      });
      expect(state.sessionStatus).toBe("running");
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

    it("keeps the sidebar row 'running' while background shells outlive the turn", () => {
      // The claude-native turn settles to idle while shells keep running; the
      // sticky tally must keep the sidebar spinner lit (the grey RunningDot),
      // matching the in-chat "N background tasks still running" indicator — not
      // fall idle the way it did before this fix.
      seedConversationsCache([conv("conv_abc", "running")]);
      useChatStore.setState({ conversationId: "conv_abc", backgroundTaskCount: 1 });
      handleSessionEvent({ type: "session_status", conversationId: "conv_abc", status: "idle" });
      expect(readConversationRows().find((c) => c.id === "conv_abc")?.status).toBe("running");
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
      skills: Array<{ name: string; description: string }>,
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

    it("ignores an event for a different conversation than the open one", async () => {
      // The store tracks one active conversation; a skills nudge for
      // another session must not fetch or touch the open composer.
      // refetchSkills' conv-id guard short-circuits before any fetch.
      useChatStore.setState({
        conversationId: "conv_open",
        skills: [{ name: "kept", description: "open session's skill" }],
      });

      handleSessionEvent({
        type: "session_skills",
        conversationId: "conv_other",
      });
      await tick();

      // Guard short-circuits: no fetch issued, open session's list intact.
      expect(fetchMock).not.toHaveBeenCalled();
      expect(useChatStore.getState().skills).toEqual([
        { name: "kept", description: "open session's skill" },
      ]);
    });

    it("drops the refetched skills when the user switched conversations mid-flight", async () => {
      // A late snapshot for a since-abandoned session must not leak its
      // skills into the now-open one (post-await conv-id re-check).
      useChatStore.setState({ conversationId: "conv_abc", skills: [] });
      fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.toString();
        if (url.split("?")[0] === "/v1/sessions/conv_abc" && (init?.method ?? "GET") === "GET") {
          // The user navigates away before the snapshot lands.
          useChatStore.setState({ conversationId: "conv_other" });
          return mockResponse({
            id: "conv_abc",
            agent_id: "agent_xyz",
            status: "idle",
            created_at: 0,
            items: [],
            skills: [{ name: "stale", description: "wrong session" }],
          });
        }
        return defaultFetchHandler(input, init);
      });

      handleSessionEvent({
        type: "session_skills",
        conversationId: "conv_abc",
      });
      await tick();

      // conv_other is now open; conv_abc's stale skills were dropped, and
      // the seed never wrote skills onto conv_other.
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
    it("reflects a TUI-side model switch in selectedModel", () => {
      // A `/model` change typed into the Claude Code terminal arrives
      // as session.model; the picker selection must follow it.
      useChatStore.setState({ conversationId: "conv_abc", selectedModel: "opus" });
      handleSessionEvent({
        type: "session_model",
        conversationId: "conv_abc",
        model: "sonnet",
      });
      expect(useChatStore.getState().selectedModel).toBe("sonnet");
    });

    it("ignores a model event from a different session", () => {
      useChatStore.setState({ conversationId: "conv_open", selectedModel: "opus" });
      handleSessionEvent({
        type: "session_model",
        conversationId: "conv_other",
        model: "sonnet",
      });
      expect(useChatStore.getState().selectedModel).toBe("opus");
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
  interface SnapshotOverrides {
    labels?: Record<string, string>;
    reasoning_effort?: string | null;
    model_override?: string | null;
    cost_control_mode_override?: "on" | "off" | null;
    parent_session_id?: string | null;
    model_options?: Array<Record<string, unknown>>;
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

  function patchCallsFor(id: string): Array<Record<string, unknown>> {
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

  it("PATCHes sticky model and effort onto a claude-native session with no overrides", async () => {
    seedSession("conv_cn", []);
    withSnapshot("conv_cn", { labels: { "omnigent.wrapper": "claude-code-native-ui" } });

    useChatStore.setState({
      selectedEffort: "high",
      selectedModel: "claude-opus-4-7",
    });
    await useChatStore.getState().switchTo("conv_cn");

    const patches = patchCallsFor("conv_cn");
    // Model is silent; effort must notify the running native session.
    expect(patches).toEqual(
      expect.arrayContaining([
        { model_override: "claude-opus-4-7", silent: true },
        { reasoning_effort: "high" },
      ]),
    );

    const state = useChatStore.getState();
    expect(state.selectedModel).toBe("claude-opus-4-7");
    expect(state.selectedEffort).toBe("high");
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

  it("PATCHes sticky model and effort onto a codex-native session with no overrides", async () => {
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
    expect(patches).toEqual(
      expect.arrayContaining([
        { model_override: "gpt-5.4", silent: true },
        { reasoning_effort: "xhigh" },
      ]),
    );

    const state = useChatStore.getState();
    expect(state.selectedModel).toBe("gpt-5.4");
    expect(state.selectedEffort).toBe("xhigh");
  });

  it("does NOT apply sticky effort or model to a sub-agent (child) session", async () => {
    // Observer sticky prefs must not overwrite child sessions.
    seedSession("conv_child", []);
    withSnapshot("conv_child", {
      labels: { "omnigent.wrapper": "claude-code-native-ui" },
      parent_session_id: "conv_parent",
    });

    useChatStore.setState({
      selectedEffort: "xhigh",
      selectedModel: "claude-opus-4-7",
    });
    await useChatStore.getState().switchTo("conv_child");

    const patches = patchCallsFor("conv_child");
    expect(patches.some((p) => "reasoning_effort" in p)).toBe(false);
    expect(patches.some((p) => "model_override" in p)).toBe(false);

    // Sticky prefs are preserved for later top-level sessions.
    const state = useChatStore.getState();
    expect(state.selectedEffort).toBe("xhigh");
    expect(state.selectedModel).toBe("claude-opus-4-7");
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
    expect(state.selectedModel).toBe("claude-sonnet-4-6");
    // The server override is the session truth shown by `/model`.
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

  it("surfaces the applied sticky model as the session override (claude-native)", async () => {
    // The claude-native handoff persists the sticky model, so it IS the
    // session's active override — `/model` should show it.
    seedSession("conv_sticky_cn", []);
    withSnapshot("conv_sticky_cn", { labels: { "omnigent.wrapper": "claude-code-native-ui" } });

    useChatStore.setState({ selectedModel: "claude-opus-4-7", sessionModelOverride: null });
    await useChatStore.getState().switchTo("conv_sticky_cn");

    expect(patchCallsFor("conv_sticky_cn")).toEqual(
      expect.arrayContaining([{ model_override: "claude-opus-4-7", silent: true }]),
    );
    expect(useChatStore.getState().sessionModelOverride).toBe("claude-opus-4-7");
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

  it("drops a pending frame's buffered blocks when the session switches mid-stream", async () => {
    useChatStore.setState({ conversationId: "conv_old", blocks: [] });
    const sink = pushableStream();
    const controller = new AbortController();
    const manual = manualScheduler();
    void pumpStreamEvents(
      "conv_old",
      sink.stream,
      controller,
      setState,
      getState,
      manual.scheduler,
    );

    sink.push(sse("response.created", { id: "resp_old", status: "in_progress", output: [] }));
    sink.push(delta("A")); // first content → sync flush onto conv_old
    sink.push(delta("B")); // buffered, frame pending
    sink.push(delta("C")); // buffered, frame pending
    await tick();
    expect(manual.pending()).toBe(true);

    // switchTo binds another session: conversationId changes + the old
    // controller aborts. The new session starts empty.
    controller.abort();
    useChatStore.setState({ conversationId: "conv_new", blocks: [] });

    // Firing the stale frame must NOT land conv_old's buffered B/C onto
    // conv_new — flush() early-returns on the conversationId mismatch.
    manual.fire();
    expect(useChatStore.getState().conversationId).toBe("conv_new");
    expect(useChatStore.getState().blocks).toEqual([]);
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

  it("returns 'switched' when the conversation changes mid-stream", async () => {
    useChatStore.setState({ conversationId: "conv_sw_a", blocks: [] });
    const sink = pushableStream();
    const done = pumpStreamEvents(
      "conv_sw_a",
      sink.stream,
      new AbortController(),
      setState,
      getState,
      immediate,
    );
    sink.push(sse("response.created", { id: "resp_sw", status: "in_progress", output: [] }));
    await tick();
    useChatStore.setState({ conversationId: "conv_sw_b" });
    // A 34-char + trailing-space delta flushes a chunk, so the next
    // for-await iteration runs the conversation-id guard and bails.
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

  it("gives up and marks the session failed on a permanent 404", async () => {
    seedSession("conv_404", []);
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (/\/v1\/sessions\/[^/]+\/stream$/.test(url)) {
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
    await vi.advanceTimersByTimeAsync(1);
    await loop;

    expect(useChatStore.getState().sessionStatus).toBe("failed");
    expect(useChatStore.getState().abortController).toBeNull();
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

  /** Drain reconcile's sequential await chain under fake timers. */
  async function drainAsync(turns = 25): Promise<void> {
    for (let i = 0; i < turns; i += 1) {
      await vi.advanceTimersByTimeAsync(1);
    }
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
      ...windowItems.map((it) => it.id),
      ...gap.map((it) => it.id),
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
    // The window was replaced wholesale with the newest page, exactly as a
    // cold bind would load it — not left with a mid-transcript hole.
    expect(state.blocks.map((b) => b.ctx.itemId)).toEqual(
      gap.slice(-SESSION_HISTORY_PAGE_SIZE).map((it) => it.id),
    );
    // The cursor was rewound to the fresh window's top, so everything older
    // (the rest of the gap included) is reachable again by paging up.
    expect(state.oldestItemId).toBe(gap.at(-SESSION_HISTORY_PAGE_SIZE)!.id);
    expect(state.hasMoreHistory).toBe(true);

    await useChatStore.getState().loadMoreHistory();
    expect(useChatStore.getState().blocks.map((b) => b.ctx.itemId)).toEqual(
      gap.slice(-2 * SESSION_HISTORY_PAGE_SIZE).map((it) => it.id),
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

  it("drops a stale reconcile/re-hydrate after a switch-away-and-back mid-backfill", async () => {
    const preGap = Array.from({ length: 30 }, (_, i) => gapUser("spre", i));
    const windowItems = preGap.slice(-SESSION_HISTORY_PAGE_SIZE);
    seedSession("conv_stale", preGap);
    seedSession("conv_other", []);
    // Route /stream opens to sinks AND hold reconcile's first backfill page.
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
    useChatStore.setState({
      conversationId: "conv_stale",
      abortController: controller,
      blocks: itemsToBlocks(windowItems),
      hasMoreHistory: true,
      oldestItemId: windowItems[0]!.id,
    });

    const loop = startStreamPump("conv_stale", controller, setState, getState);
    await drainAsync();
    expect(sinks).toHaveLength(1);

    // 100 gap items: if the stale backfill resumed, it would outrun the
    // page cap and fall through to the window re-hydrate.
    const gap = Array.from({ length: 100 }, (_, i) => gapUser("sgap", i));
    seedSessionItems("conv_stale", [...preGap, ...gap]);
    sinks[0]!.error();
    await drainAsync();
    // Sync gate: reconcile is parked on its first deferred backfill page.
    expect(releaseBackfill).not.toBeNull();

    // The user leaves and comes back: the revisit hydrates a FRESH window
    // (the newest 20 gap items) and then scrolls one page up.
    const away = useChatStore.getState().switchTo("conv_other");
    await drainAsync(5);
    await away;
    const back = useChatStore.getState().switchTo("conv_stale");
    await drainAsync(5);
    await back;
    await useChatStore.getState().loadMoreHistory();
    const fresh = useChatStore.getState();
    expect(fresh.blocks.map((b) => b.ctx.itemId)).toEqual(
      gap.slice(-2 * SESSION_HISTORY_PAGE_SIZE).map((it) => it.id),
    );

    // The stale page resolves: the conversation id matches again, so only
    // the generation guard separates it from the new window.
    releaseBackfill!();
    await drainAsync();

    const state = useChatStore.getState();
    // The stale flow must write nothing — pre-fix it ran on to the
    // re-hydrate fallback, which rewound the scroll-up cursor to the
    // window top and bumped the generation (voiding future legit pages).
    expect(state.blocks.map((b) => b.ctx.itemId)).toEqual(
      gap.slice(-2 * SESSION_HISTORY_PAGE_SIZE).map((it) => it.id),
    );
    expect(state.oldestItemId).toBe(gap.at(-2 * SESSION_HISTORY_PAGE_SIZE)!.id);
    expect(state.historyGeneration).toBe(fresh.historyGeneration);

    // The new window still pages older from where it left off: pre-fix the
    // rewound cursor re-fetched the already-rendered page (all dupes) and
    // this would still show only 40 items.
    await useChatStore.getState().loadMoreHistory();
    expect(useChatStore.getState().blocks.map((b) => b.ctx.itemId)).toEqual(
      gap.slice(-3 * SESSION_HISTORY_PAGE_SIZE).map((it) => it.id),
    );

    // Unpark the orphaned pump so the awaited loop can exit.
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
      ...gap.slice(-SESSION_HISTORY_PAGE_SIZE).map((it) => it.id),
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
  function livePreviews(): Array<Extract<AnyBlock, { type: "text_done" }>> {
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
    // survived the reconnect and the fresh pump (whose high-water index
    // map starts empty) appended the cumulative replay onto it.
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

    // The message commits while the socket is dead: the server retires
    // it from the replay index (no replay on the next connection) and
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

  it("replaces the provisional in place with the authoritative item", async () => {
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
    // id) is the only assistant text — no duplicate. If the replacement
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

    // The provisional preview must be retired and replaced — exactly one
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

  it("keeps streamed text ABOVE an elicitation that arrives mid-stream", async () => {
    // The user-reported bug: text streams, then a tool-permission card
    // arrives and must render BELOW the text that preceded it (not above).
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

    // When the authoritative item commits, it replaces the preview IN
    // PLACE — so the committed text STAYS above the elicitation rather
    // than appending after it (where the slow transcript item would land).
    sink.push(messageDone("ci_1", "resp_l", "Let me check that."));
    await tick();
    const textIdx = idxOf((b) => b.ctx.itemId === "ci_1");
    const elicitIdx2 = idxOf((b) => b.type === "elicitation");
    expect(textIdx).toBeGreaterThanOrEqual(0);
    expect(textIdx).toBeLessThan(elicitIdx2);
    expect(provisional()).toBeUndefined();

    controller.abort();
  });

  it("replaces each message's preview in place across a multi-message turn", async () => {
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

    // m2 is the only in-flight preview (FIFO replacement took m1, not m2).
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

  it("appends in-order chunks and ignores a duplicate index", async () => {
    useChatStore.setState({
      conversationId: "conv_live6",
      blocks: [],
      isNativeTerminalSession: true,
    });
    const { sink, controller } = startPump("conv_live6");

    sink.push(sse("response.created", { id: "resp_l", status: "in_progress", output: [] }));
    sink.push(nativeDelta("m1", 0, "Hello ", false));
    sink.push(nativeDelta("m1", 1, "world", true));
    // A replayed chunk at an already-applied index is a no-op.
    sink.push(nativeDelta("m1", 1, "world", true));
    await tick();

    expect(provisional()?.fullText).toBe("Hello world");

    controller.abort();
  });

  it("drops a trailing chunk that arrives after the message was finalized", async () => {
    // Regression: the forwarder can emit a message's last chunk just
    // AFTER its authoritative text_done (the chunk was written to the
    // deltas file moments before the transcript flushed). Replaying it
    // would re-create the finalized message's preview as a duplicate,
    // stale bubble that also sits below any later elicitation card.
    useChatStore.setState({
      conversationId: "conv_live7",
      blocks: [],
      isNativeTerminalSession: true,
    });
    const { sink, controller } = startPump("conv_live7");

    sink.push(sse("response.created", { id: "resp_l", status: "in_progress", output: [] }));
    sink.push(nativeDelta("m1", 0, "Hello ", false));
    await tick();
    // The authoritative final text commits and replaces + retires m1.
    sink.push(messageDone("ci_1", "resp_l", "Hello world"));
    await tick();
    expect(provisional()).toBeUndefined();

    // The trailing chunk for the already-finalized m1 arrives late.
    sink.push(nativeDelta("m1", 1, "world", true));
    await tick();

    // It is dropped: no re-created provisional (which would duplicate the
    // committed text and linger below later cards).
    expect(provisional()).toBeUndefined();
    const dones = useChatStore.getState().blocks.filter((b) => b.type === "text_done");
    expect(dones).toHaveLength(1);

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
    for (let i = 0; i < turns; i += 1) {
      await vi.advanceTimersByTimeAsync(1);
    }
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

  it("does not flush while busy (streaming or running/waiting)", () => {
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
    // Draining background work.
    useChatStore.setState({ ...base, status: "idle", sessionStatus: "waiting" });
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
});

describe("chatStore — background cross-session flush", () => {
  /** /events POSTs the flush fired, as (conversationId, text) pairs. */
  const eventPosts = (): Array<{ id: string; text: string }> =>
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

  it("serializes a background flush behind an in-flight foreground send (FIFO across paths)", async () => {
    // The navigate-away race: a foreground send() for the active conversation is
    // still in flight (its /events POST held open) when the background flush
    // fires for another conversation. Both POSTs must go through the one send
    // chain, so the background POST cannot overtake the foreground one — it
    // waits until the foreground POST resolves.
    seedConversationsCache([conv("conv_active", "idle"), conv("conv_bg", "idle")]);
    useChatStore.setState({
      conversationId: "conv_active",
      boundAgentId: "agent_xyz",
      abortController: new AbortController(),
      status: "idle",
      sessionStatus: "idle",
      queuedMessages: [{ queueId: "q_1", text: "bg-msg", conversationId: "conv_bg" }],
    });

    // Hold conv_active's foreground POST open; conv_bg's background POST resolves
    // immediately. Records delivery order across both endpoints.
    const delivered: string[] = [];
    let releaseForeground: () => void = () => {};
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url === "/v1/sessions/conv_active/events" && init?.method === "POST") {
        delivered.push("foreground");
        return new Promise<Response>((resolve) => {
          releaseForeground = () => resolve(mockResponse({ queued: true, item_id: "ci_fg" }));
        });
      }
      if (url === "/v1/sessions/conv_bg/events" && init?.method === "POST") {
        delivered.push("background");
        return mockResponse({ queued: true, item_id: "ci_bg" });
      }
      return defaultFetchHandler(input, init);
    });

    // Foreground send() takes the first chain slot and its POST is held open.
    const fg = useChatStore.getState().send("fg-msg", "agent_xyz");
    await tick();
    expect(delivered).toEqual(["foreground"]);

    // Background flush fires while the foreground POST is still in flight. It
    // must NOT deliver yet — it's queued behind the foreground POST on the chain.
    useChatStore.getState().flushBackgroundQueues();
    await tick();
    await tick();
    expect(delivered).toEqual(["foreground"]);

    // Release the foreground POST → the background POST is now free to deliver.
    releaseForeground();
    await fg;
    await tick();
    await tick();
    expect(delivered).toEqual(["foreground", "background"]);
  });
});
