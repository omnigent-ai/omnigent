// Module-scope Zustand store for the active chat session.
//
// The streaming state lives outside the React tree so it survives
// component remounts, route changes, and any UI shuffling. The
// session SSE stream is the source of truth — `switchTo` owns its
// lifecycle, opening `GET /v1/sessions/{id}/stream` on bind and
// pumping events into `state.blocks` via the BlockStream reducer.
// `send` POSTs a single event to the session; the open stream
// delivers the response.
//
// Data model:
//   - `blocks: AnyBlock[]` — committed history + streaming output.
//     The renderer walks this. Bind hydration prepends the committed
//     item history; the live pump appends stream-delivered blocks at
//     the end. Dedupe by `ctx.itemId` so stream-delivered persisted
//     items don't double-render alongside hydrated ones.
//   - `pendingUserMessages` — user inputs that have been POSTed but
//     not yet observed via `session.input.consumed`. Held off `blocks`
//     so streaming output from a prior turn can append cleanly at the
//     end without needing special positional logic. The renderer
//     displays them as user bubbles after the `blocks`-derived ones,
//     and they migrate into `blocks` (plain append) the moment their
//     `session.input.consumed` event arrives.
//
// Actions:
//   send(text, agentId, opts)
//     POSTs `{type: "message", ...}` to /events. The server returns
//     the persisted item id synchronously; we push it onto
//     `pendingUserMessages` so the bubble renders immediately. For a
//     brand-new session, we first createSession + bindStream, then
//     navigate via opts.onConversationCreated, then post. For an
//     existing session whose stream has died (idle proxy disconnect),
//     we rebind before posting so the response events aren't published
//     into an empty subscriber set.
//   stop()
//     POSTs `{type: "interrupt"}` to /events. The local stream
//     stays open; the server emits `session.interrupted` and
//     `response.incomplete` which the pump handles.
//   switchTo(convId)
//     Single owner of stream-bind. Aborts the prior stream, resets
//     state, then for non-null `convId` opens the new session's
//     stream, fetches the items snapshot, and merges into blocks
//     deduping by item id.

import type { InfiniteData, QueryClient } from "@tanstack/react-query";
import { create } from "zustand";
import type {
  AnyBlock,
  ElicitationBlock,
  ErrorBlock,
  MessageContentBlock,
  TextDone,
  UserMessageBlock,
} from "@/lib/blocks";
import { BlockStream } from "@/lib/blockStream";
import { itemsToBlocks } from "@/lib/itemsToBlocks";
import {
  ApiError,
  approve as approveElicitation,
  bindOnlyOnlineRunner,
  createSession,
  getSessionSlim,
  fetchInitialHistoryWindow,
  fetchSessionItemsPage,
  interrupt as interruptSession,
  openSessionStream,
  postEvent,
  type SessionItemsPage,
  updateSession,
} from "@/lib/sessionsApi";
import type { SessionInputConsumedEvent, SessionViewer, StreamEvent } from "@/lib/events";
import { createPresenceIdleTracker } from "@/lib/presenceIdle";
import { parseEvent, parseSseStream, type SseStreamResult } from "@/lib/sse";
import { childSessionsQueryKey, type ChildSessionInfo } from "@/hooks/useChildSessions";
import type { Conversation, ConversationsPage } from "@/hooks/useConversations";
import { useTerminalActivityStore } from "./terminalActivity";
import {
  terminalInfoFromResource,
  terminalsQueryKey,
  type TerminalInfo,
} from "@/hooks/useTerminals";
import type {
  ContentBlock,
  CodexModelOption,
  ModelUsage,
  PendingInput,
  SandboxStatus,
  Session,
  SessionStatus,
  SkillSummary,
} from "@/lib/types";
import { uploadFile } from "@/lib/filesApi";
import type { ActiveResponse } from "./types";
import { supportsEffortControl } from "@/lib/sessionCapabilities";
import { isClaudeNativeModel } from "@/lib/claudeNativeModels";
import { isCodexNativeModel } from "@/lib/codexNativeModels";
import { getCurrentAuthorId } from "@/lib/identity";
import { isNativeWrapper } from "@/lib/nativeCodingAgents";

export interface SendOptions {
  /**
   * Fires synchronously after `createSession` returns for a brand-new
   * session (before the first message is posted). Callers use this
   * to navigate `/` → `/c/:newId`. ChatPage's URL effect calls
   * `switchTo(newId)`, which no-ops via the same-id guard because
   * `send` already set `conversationId` before the callback.
   */
  onConversationCreated?: (conversationId: string) => void;
}

/**
 * A user message awaiting its `session.input.consumed` event.
 *
 * Inserted by `send` before the POST is awaited so the bubble renders
 * immediately, then matched FIFO by the consumed handler. FIFO works
 * here because client posts and server consumed events are both
 * strictly ordered within one session — we don't need to correlate by
 * itemId, which would force us to wait for the POST response and
 * miss any consumed event that races ahead of it.
 *
 * `tempId` is a client-only identifier used for two things: rollback
 * on POST failure (filter the array by tempId) and React keying of
 * the pending bubble. It is NOT the server-assigned item id — the
 * real id comes from the consumed event when we promote into `blocks`.
 */
export interface PendingUserMessage {
  tempId: string;
  content: MessageContentBlock[];
  /** Author email for this pending message. Set at send time for fresh sends; set from the snapshot's created_by for replayed entries (which may differ from the current viewer). Used as fallback when session.input.consumed arrives without created_by (native-terminal path). */
  author?: string;
  /**
   * Whether this send's POST has settled (the server accepted it).
   * From that point the server can account for the message — a native
   * send is replayed by the snapshot's `pending_inputs` until its
   * round-trip commits it; a non-native send is already persisted — so
   * `switchTo` must NOT stash a posted bubble across navigation. A
   * stale client copy would resurrect a bubble the server has since
   * resolved (committed + consumed-event missed while away), which
   * nothing can ever clear: the stuck-forever pending message. Unset
   * on snapshot-replayed entries (they're already server-owned).
   */
  posted?: boolean;
}

/**
 * A conversation's in-flight optimistic bubbles, stashed so they survive
 * in-app navigation. See {@link ChatState.pendingByConversation}.
 */
export interface StashedPending {
  /**
   * This client's own optimistic bubbles (``pend_`` temp ids) whose POST
   * has NOT settled yet — the one state the server cannot replay from
   * ``pending_inputs`` because it hasn't been told about the message.
   * Settled bubbles are deliberately excluded: the server owns those,
   * and the navigate-back snapshot re-seeds them (or shows them
   * committed).
   */
  messages: PendingUserMessage[];
  /**
   * Normalized committed user-message texts present in `blocks` at the
   * moment these bubbles were stashed (navigate-away). On navigate-back,
   * `bindStream` treats a committed message as the now-persisted copy of a
   * stashed bubble ONLY when it is NEW relative to this baseline — i.e. the
   * snapshot has MORE committed copies of that text than were here when we
   * left. Without the baseline, an optimistic bubble whose text coincides
   * with an OLDER message already in history would be wrongly deduped away
   * (the "disappears then reappears" bug on a resumed/disconnected session,
   * which always has prior history).
   */
  committedTexts: string[];
}

export interface ChatState {
  // Reactive — subscribed to by UI components.
  conversationId: string | null;
  /**
   * Flat block list (history + streaming). Renderer walks this.
   *
   * Terminal-observed (claude-native) live streaming inserts a
   * provisional `text_done` block keyed `live:<messageId>` at the
   * position its first chunk arrived, updated in place as chunks stream
   * and replaced by the authoritative item when it commits. Keeping the
   * preview in `blocks` (not a separate lane) is what makes a later
   * tool/elicitation card render below it. See `applyLiveDelta` and the
   * `text_done` branch of `pumpStreamEvents`.
   */
  blocks: AnyBlock[];
  /** User messages POSTed but not yet acked via session.input.consumed. */
  pendingUserMessages: PendingUserMessage[];
  /**
   * In-flight optimistic bubbles stashed per conversation so they survive
   * in-app navigation (`switchTo`), keyed by conversation id.
   *
   * Scope: ONLY sends whose POST hasn't settled (`posted` unset). Until
   * the POST returns, the message exists nowhere on the server — on a
   * cold-starting runner the POST is held open for the whole ensure
   * probe, before `pending_inputs.record()` runs — so navigating away
   * and back in that window would otherwise drop the user's message
   * entirely. Once the POST settles the server is the source of
   * truth: the navigate-back snapshot re-seeds the bubble from
   * `pending_inputs` while it's still queued, and shows it committed
   * once the round-trip lands. Stashing a settled bubble is what made
   * the stuck-forever pending message possible (committed while away +
   * consumed event missed + image-only content the text dedupe can't
   * match), so `send` prunes an entry from here the moment its POST
   * settles. `bindStream` dedupes a restored bubble against committed
   * messages that are NEW since the bubble was stashed (the round-trip
   * can outrun the POST response — see
   * {@link StashedPending.committedTexts}). Pruned to non-empty entries.
   */
  pendingByConversation: Record<string, StashedPending>;
  /** Lifecycle of the most recent send. `null` when idle pre-send. */
  activeResponse: ActiveResponse | null;
  /**
   * Response ids whose assistant bubbles should remain labelled cancelled.
   *
   * Native terminal integrations can persist a partial assistant message after
   * the active-response sidecar has moved on. Keeping this small durable list
   * lets the renderer label that persisted partial as interrupted by response
   * id instead of relying only on the transient `activeResponse`.
   */
  interruptedResponseIds: string[];
  status: "idle" | "streaming";
  /**
   * Server-side session status, driven by `session.status` SSE events.
   *
   * Distinct from `status` (which is a UI-local "is a send in flight"
   * flag): `sessionStatus` tracks whether the agent loop is actually
   * running on the server. Adds the `waiting` state — surfaces while
   * the parent agent loop is parked on the async-work drain
   * (background tools / sub-agents) — which the local `status` flag
   * cannot represent.
   *
   * Seeded from the snapshot on bind so a refresh on a running session
   * shows "Working…" immediately. Updated by `session.status` SSE events
   * for the rest of the session lifetime.
   */
  sessionStatus: SessionStatus;
  /**
   * Whether the active session is a native-terminal wrapper
   * (claude-native / codex-native), derived from the `omnigent.wrapper`
   * label on bind. Web messages on these sessions are NOT persisted at
   * POST time — they round-trip through the vendor TUI and reconcile via
   * the transcript forwarder's `session.input.consumed` event, which can
   * arrive AFTER a transient `idle`/`failed` status. The `session.status`
   * handler reads this to avoid clearing the optimistic bubble before its
   * consumed event lands (see that handler). `false` on `/`, before the
   * snapshot resolves, and for non-native sessions.
   */
  isNativeTerminalSession: boolean;
  /**
   * Server-bound agent id for the active conversation, read from
   * `GET /v1/sessions/{id}.agent_id` during bind. `null` while the
   * snapshot is in flight, on `/`, or for legacy conversations that
   * pre-date the sessions API and have no agent binding.
   */
  boundAgentId: string | null;
  /**
   * Human-readable name of the bound agent, read from
   * `GET /v1/sessions/{id}.agent_name` during bind. `null` while the
   * snapshot is in flight, on `/`, or when the agent row is missing.
   */
  boundAgentName: string | null;
  /** True while `switchTo` is fetching session metadata and the first history page. */
  loadingConversation: boolean;
  /** Error from the snapshot fetch in `switchTo`, if any. */
  conversationLoadError: Error | null;
  /**
   * Sticky picker pick — applies to the current session via PATCH and
   * survives navigation + reload (localStorage). ``null`` means the
   * agent-spec default applies.
   */
  selectedEffort: string | null;
  /**
   * Same shape as ``selectedEffort`` but for the LLM model. ``null``
   * falls back to the agent's ``llmModel``.
   */
  selectedModel: string | null;
  /**
   * The active session's REAL model override (server ``model_override``):
   * what the next turn actually uses, ``null`` when none and the agent
   * ``llmModel`` default applies. Session-scoped (NOT a sticky pick):
   * hydrated from the session snapshot on bind and kept in sync on
   * ``setModel`` / terminal ``/model`` switches. Distinct from
   * ``selectedModel`` (a single global sticky pick kept for cross-session
   * restore) so the ``/model`` readout never shows an unapplied sticky
   * pick as an active "(override)".
   */
  sessionModelOverride: string | null;
  /**
   * Per-session cost-control switch for the active session: ``"on"``
   * activates the spec's configured cost-control mode, ``"off"``
   * disables cost control, ``null`` defers to the spec default.
   * Session-scoped (NOT a sticky pick): hydrated from the session
   * snapshot on bind and written through `setCostControlMode`.
   */
  costControlModeOverride: "on" | "off" | null;
  /**
   * True when older items exist before the loaded history window. Binds
   * hydrate only the most recent page (see `fetchSessionItemsPage`);
   * scroll-up `loadMoreHistory` pages older until this goes false.
   */
  hasMoreHistory: boolean;
  /** True while a `loadMoreHistory` fetch is in flight. */
  loadingMoreHistory: boolean;
  /**
   * The item id at the start of the current `blocks` history window —
   * used as the `before` cursor for the next `loadMoreHistory` page
   * fetch. `null` until the first snapshot is hydrated.
   */
  oldestItemId: string | null;
  /** Bubble that should pulse briefly (highlight on nav jump). */
  flashItemId: string | null;
  /**
   * LLM model identifier from the bound agent's spec for the active
   * session, e.g. ``"anthropic/claude-sonnet-4-6"``. Populated from
   * the session snapshot on bind; ``null`` before bind or when the
   * agent has no explicit model.
   */
  llmModel: string | null;
  /**
   * Effective brain harness for the active session (override-aware),
   * e.g. ``"claude-sdk"`` or ``"pi"``. Populated from the session
   * snapshot on bind; drives the composer pill's harness suffix.
   */
  sessionHarness: string | null;
  /**
   * Context window size in tokens for the active session's model,
   * as looked up server-side. ``null`` before bind or when the
   * model is not in litellm's registry.
   */
  contextWindow: number | null;
  /**
   * Provider-reported input token count from the most recent
   * ``response.completed`` SSE event's ``usage.input_tokens``.
   * Authoritative (not an estimate). ``null`` until the first
   * completed response arrives in this session.
   */
  tokensUsed: number | null;
  /**
   * Cumulative session spend in USD, server-computed (the same total
   * the cost-budget policy gates on). Seeded from the session snapshot
   * and updated by ``session.usage`` SSE events. ``null`` when the
   * session is **unpriced** — no turn has been priced yet — so the UI
   * renders "—" rather than a misleading ``$0.00``.
   */
  sessionCostUsd: number | null;
  /**
   * Per-model usage breakdown over the active session's subtree (itself +
   * sub-agents), keyed by raw harness model id. Seeded from the session
   * snapshot on bind and replaced wholesale by ``session.usage`` SSE events
   * that carry a per-model change (an event without it leaves the cached
   * map untouched). ``null`` until per-model usage is recorded. The
   * agent-info popover renders this directly; any aggregate view (total
   * tokens, total cost) is derived from this map on the frontend.
   */
  sessionUsageByModel: Record<string, ModelUsage> | null;
  /**
   * Worktree branch checked out for the active session, surfaced in the
   * composer status line. Seeded from the session snapshot on bind
   * (stable per session). ``null`` before bind or when the session uses
   * no worktree.
   */
  gitBranch: string | null;
  /**
   * Current Claude Code todo list for `omnigent claude` sessions.
   * Populated from the session snapshot on bind and updated by
   * `session.todos` SSE events. Empty array for non-claude-native
   * sessions or before the first poll tick from the forwarder.
   */
  todos: Array<{
    content: string;
    status: "pending" | "in_progress" | "completed";
    activeForm: string;
  }>;
  /**
   * Skills the bound agent can invoke (bundled + host-discovered).
   * Populated from the session snapshot on bind; empty array
   * before bind. The composer's slash-command menu reads this to
   * suggest ``/skill-name``.
   */
  skills: SkillSummary[];
  /**
   * Codex app-server model options for the active codex-native session.
   * Populated from the session snapshot and updated when the server's
   * background Codex ``model/list`` fetch lands.
   */
  codexModelOptions: CodexModelOption[];
  /**
   * True while the runner is auto-creating the terminal for a
   * terminal-first session (claude-native / codex-native). Seeded from
   * the session snapshot's `terminal_pending` field on bind and
   * updated by `session.terminal_pending` SSE events. Drives the
   * spinner on the Terminal pill; once false, the UI relies purely on
   * whether a terminal resource exists. Always false for
   * non-terminal-first sessions.
   */
  terminalPending: boolean;
  /**
   * Users currently viewing this session (presence circles in the
   * chat header). Replaced wholesale by every `session.presence` SSE
   * event — the wire protocol is full-state, never deltas — and
   * seeded by the stream's snapshot-on-connect. Includes the current
   * user themself; display components filter self out. Reset on
   * `switchTo` so a stale list never bleeds across conversations.
   */
  viewers: SessionViewer[];
  /**
   * Managed-sandbox launch progress for the bound session. Seeded
   * from the session snapshot's `sandbox_status` field on bind and
   * updated by `session.sandbox_status` SSE events; a `ready` event
   * clears it back to `null`. Drives the provisioning indicator on
   * the session page. Always `null` for sessions without a managed
   * launch.
   */
  sandboxStatus: SandboxStatus | null;

  // Internal mutable bookkeeping. NOT meant to be subscribed to.
  abortController: AbortController | null;
  /**
   * Monotonic guard for the loaded history window. Bumped whenever the
   * window is reset (`switchTo`, `bindStream` hydration, the reconnect
   * re-hydrate fallback) so an in-flight window read (`loadMoreHistory`,
   * `reconcileOnReconnect` and its re-hydrate fallback) fetched against
   * a previous window is dropped instead of writing a stale page or
   * cursor into the new one.
   */
  historyGeneration: number;

  // Actions.
  send: (text: string, agentId: string, files?: File[], opts?: SendOptions) => Promise<void>;
  /**
   * Invoke a skill by posting a ``slash_command`` event — the same wire
   * shape the REPL sends. The server resolves the skill, persists the
   * visible receipt + hidden ``<skill>`` meta message, and forwards the
   * meta to the runner. Use this only for in-process harnesses;
   * native-terminal sessions (claude-native / codex-native) keep sending
   * plaintext so the vendor TUI loads the skill itself.
   *
   * :param name: Skill name without the leading ``/``, e.g. ``"grill-me"``.
   * :param args: Raw argument text typed after the command, ``""`` if none.
   */
  sendSlashCommand: (
    name: string,
    args: string,
    agentId: string,
    opts?: SendOptions,
  ) => Promise<void>;
  stop: () => void;
  switchTo: (conversationId: string | null) => Promise<void>;
  submitApproval: (
    elicitationId: string,
    action: "accept" | "decline" | "cancel",
    content?: Record<string, unknown>,
  ) => Promise<void>;
  /**
   * Set sticky effort; PATCH only when the active session supports it.
   * ``null`` clears the override.
   */
  setEffort: (effort: string | null) => Promise<void>;
  /**
   * Set the sticky model and PATCH it onto the current session. For
   * claude-native, the server also injects ``/model`` into the tmux
   * pane so the in-binary picker tracks the change.
   */
  setModel: (model: string | null) => Promise<void>;
  /**
   * Set the active session's cost-control switch — optimistic local
   * flip, then PATCH; the server's canonical value (or a rollback on
   * failure) settles the state. ``null`` clears back to the spec
   * default. No-ops when there is no active conversation.
   */
  setCostControlMode: (mode: "on" | "off" | null) => Promise<void>;
  /**
   * Fetch the next page of older messages and prepend them to `blocks`.
   *
   * No-ops when `hasMoreHistory` is false, `loadingMoreHistory` is true,
   * or there is no active conversation / oldest-item cursor yet.
   */
  loadMoreHistory: () => Promise<void>;
  /** Flash a bubble briefly; rapid calls reschedule so the latest target wins. */
  flashUserMessage: (itemId: string) => void;
  /**
   * Compact the active session's context. Posts a ``compact`` event to the
   * server, which summarises the conversation history in-place. No-ops when
   * there is no active conversation.
   */
  compact: () => Promise<void>;
  /**
   * Refetch runner-backed session state for the active conversation.
   *
   * Used when a native runner comes online after being unreachable: the
   * runner-owned fields (skills, Codex model catalog, terminal/session
   * metadata) may have changed while the browser only had a stale cached
   * snapshot. No-ops for inactive or missing conversations.
   */
  refreshSessionState: (conversationId?: string) => Promise<void>;
}

let queryClient: QueryClient | null = null;
let pendingSeq = 0;
// Tail of the send chain. Each `send` waits on the previous send's network
// work before issuing its own POST, so rapid-fire messages reach the server
// in submission order. Concurrent `fetch` POSTs have no ordering guarantee,
// which otherwise lets the server accept messages out of order. Module-level
// (one active chat at a time); the chain only ever resolves, never rejects.
let sendChain: Promise<void> = Promise.resolve();
let flashTimer: ReturnType<typeof setTimeout> | null = null;
const workspaceInvalidationTimers = new Map<string, ReturnType<typeof setTimeout>>();

// Must match the @keyframes user-msg-flash duration in index.css.
const FLASH_DURATION_MS = 800;
const WORKSPACE_INVALIDATION_DEBOUNCE_MS = 750;

// Reconnect backoff for the session SSE stream. Databricks Apps' ingress
// hard-caps a single HTTP/2 stream at ~5 min, so the client must re-subscribe
// when it's dropped. Backoff applies only between consecutive failed opens
// (see nextReconnectDelay); a drop after a healthy connection reconnects
// instantly.
const STREAM_RECONNECT_BASE_MS = 250;
const STREAM_RECONNECT_MAX_MS = 5_000;

// Sticky picker prefs — persisted so a new chat inherits the user's
// last pick across reloads and across sessions.
const PICKER_PREF_EFFORT_KEY = "omnigent.picker.effort";
const PICKER_PREF_MODEL_KEY = "omnigent.picker.model";

function loadPickerPref(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function savePickerPref(key: string, value: string | null): void {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    // Ignore — running without storage just means prefs don't survive reload.
  }
}

/**
 * Initialize the store with the app's QueryClient. Called once at app
 * boot from `main.tsx`. Without this the store can't fetch items
 * through the cache or invalidate the conversations query when a new
 * conversation is created.
 */
export function initChatStore(client: QueryClient): void {
  for (const timer of workspaceInvalidationTimers.values()) {
    clearTimeout(timer);
  }
  workspaceInvalidationTimers.clear();
  queryClient = client;
}

function scheduleWorkspaceFilesystemInvalidation(sessionId: string): void {
  if (workspaceInvalidationTimers.has(sessionId)) return;
  const timer = setTimeout(() => {
    workspaceInvalidationTimers.delete(sessionId);
    queryClient?.invalidateQueries({
      queryKey: ["workspace-changed-files", sessionId],
    });
    queryClient?.invalidateQueries({
      queryKey: ["workspace-all-files", sessionId],
    });
    queryClient?.invalidateQueries({
      queryKey: ["workspace-dir", sessionId],
      refetchType: "none",
    });
    queryClient?.invalidateQueries({
      queryKey: ["workspace-dir-listing", sessionId],
      refetchType: "none",
    });
    // Environment availability (root/home → the Files tab gate) can
    // change too: the post-switch runner reset publishes this same event
    // after closing the old agent's cached OSEnv, so an os_env-boundary
    // agent switch must refetch availability or the tab stays stale for
    // the query's 60 s staleTime. Active in AppShell, so the default
    // refetch flips the tab promptly.
    queryClient?.invalidateQueries({
      queryKey: ["workspace-environment", sessionId],
    });
  }, WORKSPACE_INVALIDATION_DEBOUNCE_MS);
  workspaceInvalidationTimers.set(sessionId, timer);
}

/**
 * First message handed off from NewChatDialog to ChatPage.
 *
 * `skill` is set when the landing composer recognised the text as an
 * invocation of one of the chosen agent's bundled skills (e.g.
 * `"/review-pr 123"`): ChatPage's auto-send then posts a
 * `slash_command` event (so the server resolves the skill) instead of
 * a plain message that would reach the agent as literal `/name` text.
 * `null` means plain text — including native-terminal sessions, where
 * the vendor CLI interprets slash commands itself.
 */
export interface PendingInitialPrompt {
  /** Sanitized full text the user typed, e.g. `"/review-pr 123"`. */
  text: string;
  /** Matched bundled-skill invocation, or `null` for a plain message. */
  skill: { name: string; args: string } | null;
  /** Attachments picked on the landing composer; sent with the plain
   *  first message. Skill invocations don't carry files (same as the
   *  in-session composer's slash-command path). */
  files?: File[];
}

// First-message handoff from NewChatDialog to ChatPage, keyed by the
// new conversation id. Lives outside the zustand state on purpose: it's
// a one-shot transport, not reactive render state, and writing it must
// not trigger a re-render of any subscriber. Replaces the old
// router-`location.state` handoff, which doesn't survive the embed's
// host-provided routing (the host router may not carry react-router
// state through navigate() → useLocation()). Both surfaces share this
// module-level singleton, so it works identically standalone and embedded.
const pendingInitialPrompts = new Map<string, PendingInitialPrompt>();

/**
 * Stash the first message for a freshly created conversation so ChatPage
 * can auto-send it once the session is ready. Called by NewChatDialog
 * immediately before it navigates to `/c/:conversationId`.
 *
 * @param conversationId The new conversation's id, e.g. `"conv_abc123"`.
 * @param prompt The user's first message (already sanitized by the
 *   dialog) plus its matched skill invocation, if any. Prompts with
 *   empty `text` are ignored so a blank prompt never queues an
 *   auto-send.
 */
export function setPendingInitialPrompt(
  conversationId: string,
  prompt: PendingInitialPrompt,
): void {
  if (!prompt.text) return;
  pendingInitialPrompts.set(conversationId, prompt);
}

/**
 * Read and remove the pending first message for a conversation. Read-once
 * (get + delete): the delete is what prevents a refresh/back from
 * replaying the prompt, replacing the old `navigate(..., { state: null })`
 * clear.
 *
 * @param conversationId The conversation id to consume for, e.g.
 *   `"conv_abc123"`.
 * @returns The stashed prompt, or `null` when none was set (or it was
 *   already consumed).
 */
export function consumePendingInitialPrompt(conversationId: string): PendingInitialPrompt | null {
  const prompt = pendingInitialPrompts.get(conversationId);
  if (prompt === undefined) return null;
  pendingInitialPrompts.delete(conversationId);
  return prompt;
}

export const useChatStore = create<ChatState>((set, get) => ({
  conversationId: null,
  blocks: [],
  pendingUserMessages: [],
  pendingByConversation: {},
  activeResponse: null,
  interruptedResponseIds: [],
  status: "idle",
  sessionStatus: "idle",
  isNativeTerminalSession: false,
  boundAgentId: null,
  boundAgentName: null,
  loadingConversation: false,
  conversationLoadError: null,
  selectedEffort: loadPickerPref(PICKER_PREF_EFFORT_KEY),
  selectedModel: loadPickerPref(PICKER_PREF_MODEL_KEY),
  sessionModelOverride: null,
  costControlModeOverride: null,
  hasMoreHistory: false,
  loadingMoreHistory: false,
  oldestItemId: null,
  flashItemId: null,
  llmModel: null,
  sessionHarness: null,
  contextWindow: null,
  tokensUsed: null,
  sessionCostUsd: null,
  sessionUsageByModel: null,
  gitBranch: null,
  todos: [],
  skills: [],
  codexModelOptions: [],
  terminalPending: false,
  viewers: [],
  sandboxStatus: null,
  abortController: null,
  historyGeneration: 0,

  send: async (text, agentId, files, opts) => {
    if (!agentId) {
      throw new Error("chatStore.send: no agentId");
    }
    // Sending while a response is already streaming is allowed — the
    // session API queues item-typed events and the server delivers them
    // into the running task's inbox. Keep `activeResponse` untouched in
    // that case so the in-flight bubble keeps its "streaming" lifecycle
    // until its own `response.completed` arrives.
    const alreadyStreaming = get().status === "streaming";
    if (!alreadyStreaming) {
      set({ status: "streaming", activeResponse: null });
    }

    // Push to `pendingUserMessages` BEFORE the POST so the bubble
    // renders immediately AND so `session.input.consumed` finds an
    // entry to promote even if the SSE event races ahead of the POST
    // response (separate TCP connections; either can resolve first).
    // FIFO promotion in the consumed handler matches this pending
    // entry to the eventual server item id.
    pendingSeq += 1;
    const tempId = `pend_${pendingSeq}`;
    const pendingFileBlocks: MessageContentBlock[] = (files ?? []).map((file) => {
      const filename = file.name || "image.png";
      return file.type.startsWith("image/")
        ? { type: "input_image" as const, file_id: `pending:${filename}`, filename }
        : { type: "input_file" as const, file_id: `pending:${filename}`, filename };
    });
    const content: MessageContentBlock[] = [
      ...pendingFileBlocks,
      ...(text.trim() ? [{ type: "input_text" as const, text }] : []),
    ];
    const selfAuthor = getCurrentAuthorId();
    set((s) => ({
      pendingUserMessages: [
        ...s.pendingUserMessages,
        { tempId, content, ...(selfAuthor !== null ? { author: selfAuthor } : {}) },
      ],
    }));

    // Pin the destination before joining the send chain: a stalled prior
    // send can delay this POST past a session switch, and resolving the
    // target afterward would leak the message into the now-active session.
    const submitConversationId = get().conversationId;

    // Take our place in the send chain: wait for the prior send's network
    // work, then hand off to the next via `releaseSend` in the finally
    // below. This serializes POSTs in submission order without delaying the
    // optimistic bubble rendered above. `priorSend` only ever resolves.
    const priorSend = sendChain;
    let releaseSend: () => void = () => {};
    sendChain = new Promise<void>((resolve) => {
      releaseSend = resolve;
    });

    // The session this send actually posts to, once resolved. Read in the
    // catch to decide whether a failure may touch the active session's UI.
    let postedSessionId: string | null = null;

    try {
      await priorSend;
      const sessionId = await ensureBoundSession(agentId, set, get, opts, submitConversationId);
      postedSessionId = sessionId;

      // Upload any attached files and build the real content blocks with
      // server-assigned file_ids. For images use input_image; for all
      // other types use input_file. Plain text (if any) appended last.
      const fileBlocks: ContentBlock[] = [];
      if (files && files.length > 0) {
        for (const file of files) {
          const uploaded = await uploadFile(sessionId, file);
          if (file.type.startsWith("image/")) {
            fileBlocks.push({
              type: "input_image",
              file_id: uploaded.id,
              filename: uploaded.filename,
            });
          } else {
            fileBlocks.push({
              type: "input_file",
              file_id: uploaded.id,
              filename: uploaded.filename,
            });
          }
        }
      }
      const serverContent: ContentBlock[] = [
        ...fileBlocks,
        ...(text.trim() ? [{ type: "input_text" as const, text }] : []),
      ];

      // Promote "pending:<filename>" to real file_ids. Claude-native's
      // session.input.consumed is text-only (transcript round-trip
      // drops input_image blocks), so the consumed handler falls back
      // to the pending file blocks — they must already carry real ids.
      if (fileBlocks.length > 0) {
        set((s) => ({
          pendingUserMessages: s.pendingUserMessages.map((p) =>
            p.tempId === tempId ? { ...p, content: serverContent } : p,
          ),
        }));
      }

      const postResult = await postEvent(sessionId, {
        type: "message",
        data: {
          role: "user",
          content: serverContent,
        },
      });
      // Policy denied the input — the server returned immediately
      // without starting a turn or persisting the user message, so
      // no session.input.consumed will reconcile this exact optimistic
      // bubble. Settle local state from the POST response instead of
      // depending on the live stream being connected.
      if (postResult.denied) {
        set((s) => {
          // The stash copy rolls back even when the user has navigated
          // away — that's exactly when the entry lives there, and a
          // denied send has no server-side record to ever reconcile it.
          const patch: Partial<ChatState> = {
            pendingByConversation: removeFromPendingStash(
              s.pendingByConversation,
              sessionId,
              tempId,
            ),
          };
          if (s.conversationId !== sessionId) return patch;
          patch.pendingUserMessages = s.pendingUserMessages.filter((p) => p.tempId !== tempId);
          if (!alreadyStreaming) {
            patch.status = "idle";
            patch.sessionStatus = "idle";
          }
          return patch;
        });
      } else {
        // POST accepted: the server can now account for this message
        // (native: pending_inputs replay until the round-trip commits
        // it; non-native: already persisted). Mark the live bubble
        // settled and drop any stash copy so navigation defers to the
        // server instead of resurrecting a client copy the server may
        // have since resolved — the stuck-pending-bubble bug. The live
        // bubble itself keeps rendering until its consumed event pops
        // it; only the navigation-survival policy changes here.
        set((s) => ({
          pendingUserMessages: s.pendingUserMessages.map((p) =>
            p.tempId === tempId ? { ...p, posted: true } : p,
          ),
          pendingByConversation: removeFromPendingStash(s.pendingByConversation, sessionId, tempId),
        }));
      }
      // Note: native-terminal messages return a `pending_id`, but the
      // optimistic bubble deliberately keeps its client temp id as its
      // stable React key — swapping it to the server id mid-send forces
      // a bubble remount (a visible flink). The eventual
      // `session.input.consumed` clears this bubble by FIFO order (its
      // `clearedPendingId` matches only snapshot-hydrated bubbles, which
      // already carry the server id); see the consumed handler.
      // Refresh the sidebar without waiting for the 4 s `useConversations`
      // poll — picks up server-side title auto-gen and any runner_id /
      // status transitions that happen during the turn.
      queryClient?.invalidateQueries({ queryKey: ["conversations"] });
    } catch (err) {
      const { message, code } = describeSendFailure(err);
      // The stash copy rolls back unconditionally: a failed send has no
      // server-side record, so a bubble stashed by a mid-POST
      // navigate-away would otherwise strand as forever-pending on
      // navigate-back (nothing can ever reconcile it).
      const stashSessionId = postedSessionId ?? submitConversationId;
      if (stashSessionId !== null) {
        set((s) => ({
          pendingByConversation: removeFromPendingStash(
            s.pendingByConversation,
            stashSessionId,
            tempId,
          ),
        }));
      }
      // A queued send can target a session the user has since switched away
      // from (submit-time pin). Only roll back the bubble and settle status
      // when that session is still active — otherwise the failure would
      // clobber the now-active session's UI. When the throw came from
      // session setup itself (postedSessionId never resolved), settle
      // unconditionally: that failure belongs to the active context.
      if (postedSessionId === null || get().conversationId === postedSessionId) {
        // Roll back the optimistic bubble — no server idle will fire.
        set((s) => ({
          pendingUserMessages: s.pendingUserMessages.filter((p) => p.tempId !== tempId),
        }));
        if (!alreadyStreaming) {
          if (get().activeResponse !== null) {
            // A response bubble already exists (the turn started, then failed)
            // — mark it failed so the error rides on that bubble.
            finalizeActive(set, "failed", message, null);
          } else {
            // No response bubble to carry the failure — the turn never started
            // (e.g. the runner never came online, so POST /events 503'd). Append
            // a standalone error block so the user sees WHY nothing happened
            // instead of being left on a silent, empty composer.
            set((s) => ({ blocks: [...s.blocks, makeClientErrorBlock(message, code)] }));
          }
          set({ status: "idle" });
        }
      }
    } finally {
      // Release the next queued send regardless of success/failure so one
      // failed POST can't stall the chain forever.
      releaseSend();
    }
  },

  sendSlashCommand: async (name, args, agentId, opts) => {
    if (!agentId) {
      throw new Error("chatStore.sendSlashCommand: no agentId");
    }
    // Mirror `send`'s lifecycle scaffolding (streaming flag + send-chain
    // serialization) so a skill invocation behaves like any other turn.
    const alreadyStreaming = get().status === "streaming";
    if (!alreadyStreaming) {
      set({ status: "streaming", activeResponse: null });
    }
    // Optimistic echo of the typed command, mirroring `send`. Without it
    // the chat shows nothing until the server's `slash_command` receipt
    // arrives over SSE — and on a fresh session that POST is held open
    // while the host boots a runner and resolves the skill, so a
    // skill-first session flashed the empty-chat state for seconds. The
    // pump's `slash_command` case pops this FIFO entry the moment the
    // receipt (and its synthesized `${id}:user` echo block) lands, so the
    // optimistic bubble swaps for the committed one in the same flush.
    pendingSeq += 1;
    const tempId = `pend_${pendingSeq}`;
    const commandText = args ? `/${name} ${args}` : `/${name}`;
    const selfAuthor = getCurrentAuthorId();
    set((s) => ({
      pendingUserMessages: [
        ...s.pendingUserMessages,
        {
          tempId,
          content: [{ type: "input_text" as const, text: commandText }],
          ...(selfAuthor !== null ? { author: selfAuthor } : {}),
        },
      ],
    }));

    // Pin the destination at submit time — see `send` above for why a late
    // resolve mis-routes to the session the user has since switched to.
    const submitConversationId = get().conversationId;

    const priorSend = sendChain;
    let releaseSend: () => void = () => {};
    sendChain = new Promise<void>((resolve) => {
      releaseSend = resolve;
    });

    // The session this command actually posts to, once resolved.
    let postedSessionId: string | null = null;

    try {
      await priorSend;
      const sessionId = await ensureBoundSession(agentId, set, get, opts, submitConversationId);
      postedSessionId = sessionId;
      // Same wire shape the REPL sends (repl/_repl.py). The server resolves
      // the skill, persists a visible receipt + hidden `<skill>` meta
      // message, and forwards the meta to the runner.
      const postResult = await postEvent(sessionId, {
        type: "slash_command",
        data: { kind: "skill", name, arguments: args },
      });
      if (postResult.denied) {
        // Denied commands publish no receipt, so nothing will pop the
        // optimistic echo — roll it back here alongside the status
        // settle. The stash copy rolls back even when the user has
        // navigated away (that's when the entry lives there).
        set((s) => {
          const patch: Partial<ChatState> = {
            pendingByConversation: removeFromPendingStash(
              s.pendingByConversation,
              sessionId,
              tempId,
            ),
          };
          if (s.conversationId !== sessionId) return patch;
          patch.pendingUserMessages = s.pendingUserMessages.filter((p) => p.tempId !== tempId);
          if (!alreadyStreaming) {
            patch.status = "idle";
            patch.sessionStatus = "idle";
          }
          return patch;
        });
      } else {
        // POST accepted: the server persisted the visible receipt, so
        // navigation can rely on the snapshot — mark the echo settled
        // and drop any stash copy. Without this, an echo stashed by a
        // mid-POST navigate-away strands forever: the receipt is a
        // SlashCommandBlock, not a user message, so the navigate-back
        // text dedupe can never match it, and no consumed event fires.
        set((s) => ({
          pendingUserMessages: s.pendingUserMessages.map((p) =>
            p.tempId === tempId ? { ...p, posted: true } : p,
          ),
          pendingByConversation: removeFromPendingStash(s.pendingByConversation, sessionId, tempId),
        }));
      }
      queryClient?.invalidateQueries({ queryKey: ["conversations"] });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      // The stash copy rolls back unconditionally — a failed command has
      // no server-side record to ever reconcile a stashed echo.
      const stashSessionId = postedSessionId ?? submitConversationId;
      if (stashSessionId !== null) {
        set((s) => ({
          pendingByConversation: removeFromPendingStash(
            s.pendingByConversation,
            stashSessionId,
            tempId,
          ),
        }));
      }
      // Only settle status when the failed command's session is still active
      // — a queued send can target a session the user switched away from, and
      // its failure must not reset the now-active session's UI. A throw from
      // session setup itself (postedSessionId unresolved) settles the active
      // context as before.
      const stillActive = postedSessionId === null || get().conversationId === postedSessionId;
      if (stillActive) {
        // Roll back the optimistic echo — no receipt will reconcile it.
        set((s) => ({
          pendingUserMessages: s.pendingUserMessages.filter((p) => p.tempId !== tempId),
        }));
      }
      if (stillActive && !alreadyStreaming) {
        finalizeActive(set, "failed", message, null);
        set({ status: "idle" });
      }
    } finally {
      releaseSend();
    }
  },

  stop: () => {
    const sessionId = get().conversationId;
    if (!sessionId) return;
    // Fire-and-forget interrupt; the server emits session.interrupted
    // + response.incomplete on the open stream, which the pump
    // translates into the cancelled bubble decoration. We deliberately
    // do NOT abort the local SSE stream — it remains open across
    // turns; switchTo or tab unload is the only thing that tears it
    // down.
    void interruptSession(sessionId).catch(() => {
      // Interrupt is best-effort. A network failure here means the
      // user's cancel won't reach the server, but the local UI already
      // reflects the user's stop request below.
    });
    set((s) => {
      if (s.conversationId !== sessionId) return {};
      const patch: Partial<ChatState> = {
        pendingUserMessages: [],
        status: "idle",
        sessionStatus: "idle",
      };
      if (s.activeResponse?.state === "streaming") {
        patch.activeResponse = {
          ...s.activeResponse,
          state: "cancelled",
          error: null,
        };
      }
      return patch;
    });
    // Optimistic, unbacked write: unlike the session.status SSE caller, no
    // server event backs this, so a poll that interleaves while the turn is
    // genuinely still running may briefly revert the sidebar dot — the helper's
    // "never fights the poller" contract doesn't hold here. Self-corrects on the
    // real idle event.
    patchConversationStatusInCache(sessionId, "idle");
    // Mirror the session.status handler: a sub-agent's row lives in its parent's
    // child-sessions list, not the sidebar, so refresh the rail in lockstep.
    const snapshot = queryClient?.getQueryData<Session>(["session", sessionId]);
    if (snapshot?.parentSessionId) {
      queryClient?.invalidateQueries({
        queryKey: childSessionsQueryKey(snapshot.parentSessionId),
      });
    }
  },

  switchTo: async (conversationId) => {
    if (get().conversationId === conversationId) return;

    // Abort the prior session's stream. The reader loop in
    // bindStream's pump unwinds via AbortError and stops applying
    // events to state.blocks.
    get().abortController?.abort();

    set((s) => {
      // Stash the OUTGOING conversation's still-in-flight optimistic
      // bubbles and restore the INCOMING one's. Until a send's POST
      // settles, the message exists nowhere on the server (a
      // cold-starting runner holds the POST open before
      // pending_inputs.record() runs), so wiping pendingUserMessages
      // here (as a cold load must) would drop the user's message
      // entirely on navigate-away. The stash bridges exactly
      // that window and nothing more. Pruned to non-empty so the map
      // doesn't accrete stale keys; a restored bubble is reconciled
      // against the server snapshot in bindStream.
      const pendingByConversation = { ...s.pendingByConversation };
      if (s.conversationId !== null) {
        // Stash only THIS client's own UNSETTLED bubbles — client temp
        // ids (`pend_<n>`, set by send) whose POST hasn't returned.
        // Everything else is server-owned and re-seeded by the
        // navigate-back snapshot: snapshot-replayed / foreign bubbles
        // (server `pending_<hex>` ids) come back via pending_inputs, and
        // settled own sends (`posted`) come back via pending_inputs while
        // queued or as committed items once their round-trip lands.
        // Stashing a settled bubble is what stranded image-only messages
        // forever (committed while away + consumed event missed → no
        // pending_inputs entry left, no text for the dedupe to match).
        const own = s.pendingUserMessages.filter(
          (p) => p.tempId.startsWith("pend_") && p.posted !== true,
        );
        if (own.length > 0) {
          // Capture the committed user texts present NOW as the dedup
          // baseline: on navigate-back only committed messages new since this
          // moment count as the persisted copy of a stashed bubble (see
          // StashedPending.committedTexts).
          pendingByConversation[s.conversationId] = {
            messages: own,
            committedTexts: committedUserTextsOf(s.blocks),
          };
        } else {
          delete pendingByConversation[s.conversationId];
        }
      }
      return {
        pendingByConversation,
        conversationId,
        // Cleared here, so a different session's in-flight preview blocks
        // (``live:*``) never bleed across.
        blocks: [],
        pendingUserMessages:
          conversationId !== null ? (pendingByConversation[conversationId]?.messages ?? []) : [],
        activeResponse: null,
        interruptedResponseIds: [],
        status: "idle",
        sessionStatus: "idle",
        isNativeTerminalSession: false,
        boundAgentId: null,
        boundAgentName: null,
        loadingConversation: conversationId !== null,
        conversationLoadError: null,
        hasMoreHistory: false,
        loadingMoreHistory: false,
        oldestItemId: null,
        llmModel: null,
        sessionHarness: null,
        // ``selectedEffort`` / ``selectedModel`` are sticky user picks —
        // not reset here so a CLI-created new chat inherits them.
        // ``sessionModelOverride`` and the cost switch ARE session-scoped,
        // so they reset with the session and re-hydrate from the snapshot.
        sessionModelOverride: null,
        costControlModeOverride: null,
        contextWindow: null,
        tokensUsed: null,
        sessionCostUsd: null,
        sessionUsageByModel: null,
        gitBranch: null,
        todos: [],
        skills: [],
        codexModelOptions: [],
        terminalPending: false,
        viewers: [],
        sandboxStatus: null,
        abortController: null,
        historyGeneration: s.historyGeneration + 1,
      };
    });

    if (conversationId === null) return;
    // hydratePending: this is a cold load / navigation. When no bubble was
    // restored from the stash above, replay the snapshot's un-consumed
    // native messages; when one was, bindStream dedupes it against the
    // committed snapshot. Reconnect/rebind paths pass false so they never
    // overwrite the live optimistic bubbles (which would flink).
    await bindStream(conversationId, set, get, true);
  },

  submitApproval: async (elicitationId, action, content) => {
    const sessionId = get().conversationId;
    if (!sessionId) return;
    const targetSessionId =
      get().blocks.find(
        (b): b is ElicitationBlock => b.type === "elicitation" && b.elicitationId === elicitationId,
      )?.targetSessionId ?? sessionId;
    // Optimistically flip the matching elicitation block to
    // "responded" so the buttons disappear immediately. No server
    // event confirms the approval — the agent just resumes (or
    // refuses) and emits its next stream events, so this local
    // update is the entire UX of "I clicked accept".
    //
    // ``content`` rides through the response field so multi-choice
    // cards (AskUserQuestion) can render the selected label rather
    // than a generic "Approved" pill.
    const responseValue: ElicitationBlock["response"] =
      content === undefined ? { action } : { action, content };
    set((s) => ({
      blocks: s.blocks.map((b) =>
        b.type === "elicitation" && b.elicitationId === elicitationId
          ? { ...b, status: "responded", response: responseValue }
          : b,
      ),
    }));
    try {
      await approveElicitation(
        targetSessionId,
        elicitationId,
        content === undefined ? { action } : { action, content },
      );
    } catch {
      // Roll back to pending so the user can retry. Surfacing the
      // error is a future affordance — for now, the buttons
      // reappear and the user can try again.
      set((s) => ({
        blocks: s.blocks.map((b) =>
          b.type === "elicitation" && b.elicitationId === elicitationId
            ? { ...b, status: "pending", response: null }
            : b,
        ),
      }));
    }
  },

  flashUserMessage: (itemId) => {
    if (flashTimer !== null) clearTimeout(flashTimer);
    set({ flashItemId: itemId });
    flashTimer = setTimeout(() => {
      flashTimer = null;
      set({ flashItemId: null });
    }, FLASH_DURATION_MS);
  },

  compact: async () => {
    const { conversationId } = get();
    if (!conversationId) return;
    await postEvent(conversationId, { type: "compact", data: {} });
  },

  refreshSessionState: async (conversationId) => {
    const id = conversationId ?? get().conversationId;
    if (!id) return;
    await refetchRunnerBackedSessionState(id, {
      refreshState: true,
      applyBindingPatch: true,
    });
  },

  setEffort: async (effort) => {
    set({ selectedEffort: effort });
    savePickerPref(PICKER_PREF_EFFORT_KEY, effort);
    const { conversationId } = get();
    if (conversationId) {
      if (queryClient === null) {
        throw new Error("chatStore.setEffort: queryClient not initialized");
      }
      const session = await queryClient.fetchQuery({
        queryKey: ["session", conversationId],
        queryFn: () => getSessionSlim(conversationId),
        staleTime: Infinity,
        retry: false,
      });
      if (!supportsEffortControl(session)) return;
      await updateSession(conversationId, { reasoningEffort: effort });
    }
  },

  setModel: async (model) => {
    // `selectedModel` is the sticky pick; `sessionModelOverride` is this
    // session's applied override. An explicit `/model` sets both.
    set({ selectedModel: model, sessionModelOverride: model });
    savePickerPref(PICKER_PREF_MODEL_KEY, model);
    const { conversationId } = get();
    if (conversationId) {
      const session = await updateSession(conversationId, { modelOverride: model });
      // Server-canonical may differ from the optimistic write (e.g.
      // when a clear alias was sent) — refresh local state to match.
      const canonical = session.modelOverride ?? null;
      set({ selectedModel: canonical, sessionModelOverride: canonical });
      savePickerPref(PICKER_PREF_MODEL_KEY, canonical);
    }
  },

  setCostControlMode: async (mode) => {
    const { conversationId } = get();
    if (!conversationId) return;
    const previous = get().costControlModeOverride;
    // Optimistic flip so the pill responds instantly; the PATCH
    // response (or the rollback below) is the settled truth.
    set({ costControlModeOverride: mode });
    try {
      const session = await updateSession(conversationId, { costControlModeOverride: mode });
      if (get().conversationId !== conversationId) return;
      set({ costControlModeOverride: session.costControlModeOverride ?? null });
    } catch (err) {
      // Roll back so the pill doesn't claim a state the server never persisted.
      if (get().conversationId === conversationId) {
        set({ costControlModeOverride: previous });
      }
      throw err;
    }
  },

  loadMoreHistory: async () => {
    const { conversationId, oldestItemId, loadingMoreHistory, hasMoreHistory, historyGeneration } =
      get();
    if (!conversationId || !oldestItemId || loadingMoreHistory || !hasMoreHistory) return;
    set({ loadingMoreHistory: true });
    // Drop the result if the window was reset while this page was in flight
    // (navigate away-and-back, rebind hydration, reconnect re-hydrate): the
    // page is cursor-relative to the OLD window, and prepending it into the
    // new one would invert order or rewind the cursor past a silent gap.
    const stale = (): boolean =>
      get().conversationId !== conversationId || get().historyGeneration !== historyGeneration;
    try {
      const { items, hasMore } = await fetchSessionItemsPage(conversationId, {
        olderThan: oldestItemId,
      });
      if (stale()) return;
      const newBlocks = itemsToBlocks(items);
      set((state) => {
        // Rebind hydration resets the cursor to the fresh window's top while
        // keeping scrolled-up blocks, so an older page can overlap — dedupe.
        const seen = new Set(
          state.blocks.map((b) => b.ctx.itemId).filter((iid): iid is string => Boolean(iid)),
        );
        const unique = newBlocks.filter((b) => !b.ctx.itemId || !seen.has(b.ctx.itemId));
        return {
          blocks: [...unique, ...state.blocks],
          hasMoreHistory: hasMore,
          oldestItemId: items[0]?.id ?? state.oldestItemId,
          loadingMoreHistory: false,
        };
      });
    } catch {
      // A stale failure must not disable scroll-up on the NEW window.
      if (stale()) return;
      // Disable further fetches on error — a persistent server failure
      // would otherwise re-trigger the scroll listener on every scroll event.
      set({ loadingMoreHistory: false, hasMoreHistory: false });
    }
  },
}));

// ── Internal helpers ─────────────────────────────────────

type Setter = (partial: Partial<ChatState> | ((state: ChatState) => Partial<ChatState>)) => void;
type Getter = () => ChatState;

type NativeModelFamily = "claude" | "codex";

/**
 * Resolve the native model family from a session wrapper label.
 *
 * :param session: Session snapshot from the API.
 * :returns: ``"claude"`` / ``"codex"`` for native wrappers, else ``null``.
 */
function nativeModelFamilyForSession(session: Pick<Session, "labels">): NativeModelFamily | null {
  switch (session.labels?.["omnigent.wrapper"]) {
    case "claude-code-native-ui":
      return "claude";
    case "codex-native-ui":
      return "codex";
    default:
      return null;
  }
}

/**
 * Whether a sticky model id can be applied to a native session family.
 *
 * :param family: Native model family from :func:`nativeModelFamilyForSession`.
 * :param model: Sticky model id / alias.
 * :returns: True only when the model is compatible with that native family.
 */
function isNativeModelCompatible(
  family: NativeModelFamily,
  model: string,
  session: Session,
): boolean {
  switch (family) {
    case "claude":
      return isClaudeNativeModel(model);
    case "codex":
      return isCodexNativeModel(session.codexModelOptions ?? [], model);
  }
}

/**
 * Ensure the store has a bound session with a live SSE stream, creating
 * one if there is no conversation yet. Returns the session id. Shared by
 * `send` and `sendSlashCommand` so the two POST entry points can't drift
 * in how they create or rebind sessions.
 *
 * :param agentId: Agent to create a fresh session for when none exists,
 *     e.g. ``"ag_abc123"``.
 * :param set: zustand setter.
 * :param get: zustand getter.
 * :param opts: Optional callbacks; ``onConversationCreated`` fires with
 *     the new session id the moment it's known (for eager URL promotion).
 * :param pinnedConversationId: Session id captured at send submit time,
 *     e.g. ``"conv_abc123"``; the send routes here even after a session
 *     switch. ``null`` / ``undefined`` falls back to the live
 *     ``conversationId`` (brand-new-chat path).
 * :returns: The bound session id.
 * :raises Error: Re-raises a ``conversationLoadError`` if a needed rebind
 *     of an existing session fails to establish the stream.
 */
async function ensureBoundSession(
  agentId: string,
  set: Setter,
  get: Getter,
  opts?: SendOptions,
  pinnedConversationId?: string | null,
): Promise<string> {
  // Use the session pinned at submit time so a queued send still targets
  // where it was composed, not wherever the user switched to meanwhile.
  // Null/undefined → live id: the brand-new-chat path where the session is
  // created here (a late read also avoids a duplicate create on the chain).
  let sessionId = pinnedConversationId ?? get().conversationId;

  if (sessionId === null) {
    // Brand-new-session path. Create empty (the route accepts
    // initial_items but we don't use them — see migration plan R13:
    // initial_items dispatch synchronously inside create_session,
    // before we can subscribe to /stream, so early events can be
    // missed). Bind the stream FIRST, then post the first message.
    const session = await createSession(agentId, []);
    sessionId = session.id;
    // Native runners read reasoning_effort during bind.
    const preBindEffort = get().selectedEffort;
    if (preBindEffort != null && supportsEffortControl(session)) {
      await updateSession(sessionId, {
        reasoningEffort: preBindEffort,
        silent: true,
      });
    }
    await bindOnlyOnlineRunner(sessionId);
    set({
      conversationId: sessionId,
      // We just created this session — set boundAgentId/Name from the
      // returned record so the picker doesn't briefly flicker
      // through `null` before bindStream's getSession resolves.
      boundAgentId: session.agentId,
      boundAgentName: session.agentName,
      loadingConversation: true,
    });
    opts?.onConversationCreated?.(sessionId);
    queryClient?.invalidateQueries({ queryKey: ["conversations"] });
    await bindStream(sessionId, set, get);
  } else if (sessionId === get().conversationId && get().abortController === null) {
    // The SSE pump is gone — most commonly an HTTP intermediary
    // closed the connection on idle. POSTing without a live pump
    // would queue the message, run the turn, and publish events
    // into an empty subscriber set; the user would never see the
    // response. Rebind first, and fail loud if the rebind itself
    // can't establish the stream. Gated on the target still being active —
    // rebinding a session the user navigated away from would clobber the
    // now-active session's view.
    set({ conversationLoadError: null });
    await bindStream(sessionId, set, get);
    const loadError = get().conversationLoadError;
    if (loadError !== null) throw loadError;
  }

  return sessionId;
}

/**
 * Parse a session snapshot's `pending_elicitations` payloads into
 * renderable elicitation blocks.
 *
 * Funnels each raw event dict through the same SSE parser + BlockStream
 * reducer the live path uses, so an ApprovalCard renders identically
 * whether the prompt arrived live, on cold load, or via the reconnect
 * reconcile. Entries that fail to parse are skipped (same policy as the
 * live stream: an unrecognized event must not break the chat).
 */
function pendingElicitationBlocksFromSnapshot(session: Session): AnyBlock[] {
  const events: StreamEvent[] = [];
  for (const raw of session.pendingElicitations ?? []) {
    const evt = parseEvent("response.elicitation_request", raw);
    if (evt !== null) events.push(evt);
  }
  return events.length > 0 ? new BlockStream().reduceSync(events) : [];
}

/**
 * Reconcile pending ApprovalCards against a fresh session snapshot.
 *
 * Re-fetches the session and flips any still-shown elicitation card whose
 * id is no longer in the snapshot's `pendingElicitations` to "resolved
 * elsewhere" — the same end state the `response.elicitation_resolved` SSE
 * event produces. This is the recovery path for a backgrounded tab that
 * missed that event (e.g. the approval was answered in the native-terminal
 * popup while the web tab was hidden). No-op when the session changed mid-
 * fetch, the fetch fails (transient — the next focus retries), or nothing
 * is stale.
 *
 * @param id - Conversation/session id to reconcile.
 */
async function reconcilePendingElicitations(id: string): Promise<void> {
  if (queryClient === null) return;
  let session: Session;
  try {
    session = await queryClient.fetchQuery({
      queryKey: ["session", id],
      queryFn: () => getSessionSlim(id),
      staleTime: 0,
      retry: false,
    });
  } catch {
    return;
  }
  if (useChatStore.getState().conversationId !== id) return;
  const stillPending = new Set(
    (session.pendingElicitations ?? [])
      .map((e) => (typeof e.elicitation_id === "string" ? e.elicitation_id : null))
      .filter((x): x is string => x !== null),
  );
  useChatStore.setState((s) => {
    let changed = false;
    const blocks = s.blocks.map((b) => {
      if (
        b.type === "elicitation" &&
        b.status === "pending" &&
        !stillPending.has(b.elicitationId)
      ) {
        changed = true;
        const updated: ElicitationBlock = {
          ...b,
          status: "responded",
          response: { action: "auto_resolved" },
        };
        return updated;
      }
      return b;
    });
    return changed ? { blocks } : {};
  });
}

/**
 * Store fields derived from the session's agent binding, computed from a
 * session snapshot.
 *
 * Shared by `bindStream` (cold load / rebind) and the
 * `session.agent_changed` refresh so the two paths can't drift on which
 * fields describe "what agent/harness is this session on". Most
 * importantly `isNativeTerminalSession`: native-terminal wrappers
 * (claude-native / codex-native) defer user-message persistence to the
 * transcript round-trip, so the `session.status` handler must not clear
 * their optimistic bubbles on a transient idle — a stale `false` here
 * after an in-place sdk→native agent switch is exactly the
 * "first message disappears then reappears" bug.
 *
 * Deliberately excludes turn-lifecycle state (`sessionStatus`,
 * `pendingUserMessages`, `blocks`) and usage counters — those are owned
 * by their own SSE events and must not be clobbered by a late snapshot.
 */
function sessionBindingPatch(
  session: Session,
): Pick<
  ChatState,
  | "isNativeTerminalSession"
  | "boundAgentId"
  | "boundAgentName"
  | "llmModel"
  | "sessionModelOverride"
  | "sessionHarness"
  | "costControlModeOverride"
  | "contextWindow"
  | "gitBranch"
  | "skills"
  | "codexModelOptions"
  | "terminalPending"
  | "sandboxStatus"
> {
  const wrapper = session.labels?.["omnigent.wrapper"];
  return {
    isNativeTerminalSession: isNativeWrapper(wrapper),
    boundAgentId: session.agentId,
    boundAgentName: session.agentName,
    llmModel: session.llmModel ?? null,
    sessionModelOverride: session.modelOverride ?? null,
    sessionHarness: session.harness ?? null,
    costControlModeOverride: session.costControlModeOverride ?? null,
    contextWindow: session.contextWindow ?? null,
    gitBranch: session.gitBranch ?? null,
    skills: session.skills ?? [],
    codexModelOptions: session.codexModelOptions ?? [],
    terminalPending: session.terminalPending ?? false,
    sandboxStatus: session.sandboxStatus ?? null,
  };
}

/**
 * Re-derive the agent-binding-dependent store state from a fresh session
 * snapshot, after a `session.agent_changed` SSE event.
 *
 * The switch-agent route mutates the session in place (new agent clone,
 * recomputed harness presentation labels) without a navigation, so the
 * URL-driven `switchTo`/`bindStream` path never re-runs — this is the
 * only thing that updates the store's binding state for an in-place
 * switch. Fetches through the shared `["session", id]` query key with
 * `staleTime: 0` so the React-query consumers (header, pickers) get the
 * fresh snapshot too. No-op when the session changed mid-fetch or the
 * fetch fails (transient — any later rebind re-derives from scratch).
 *
 * @param id - Conversation/session id whose binding changed.
 */
async function refreshSessionBinding(id: string): Promise<void> {
  if (queryClient === null) return;
  let session: Session;
  try {
    session = await queryClient.fetchQuery({
      queryKey: ["session", id],
      queryFn: () => getSessionSlim(id),
      staleTime: 0,
      retry: false,
    });
  } catch {
    return;
  }
  if (useChatStore.getState().conversationId !== id) return;
  useChatStore.setState(sessionBindingPatch(session));
}

/**
 * Start the session SSE stream, kick off the pump in the background
 * once the stream connects, then fetch metadata plus the most recent
 * page of item history and merge it into state.blocks.
 *
 * Order matters per the migration plan §R1 ("stream-then-snapshot
 * race") — start the stream request FIRST so events emitted during
 * the history fetch window have a live-tail request to land on.
 * Do not await the stream response before loading the snapshot:
 * proxies can delay SSE headers until data arrives, and pending
 * elicitations must still replay on refresh while the stream is
 * connecting. Dedupe by item id on merge so stream-delivered
 * persisted items don't double-render alongside hydrated ones.
 */
async function bindStream(
  id: string,
  set: Setter,
  get: Getter,
  hydratePending = false,
): Promise<void> {
  const controller = new AbortController();
  set({ abortController: controller });

  void startStreamPump(id, controller, set, get);

  // Background tabs can miss the `response.elicitation_resolved` SSE event
  // (browser throttling), so a pending ApprovalCard that was answered on
  // another surface (e.g. the native-terminal popup) would stay stuck until
  // a refresh. When the tab becomes visible again, reconcile against a fresh
  // snapshot so any no-longer-pending card flips to resolved. Removed when
  // the stream unbinds (abort), so it never leaks across conversations.
  if (typeof document !== "undefined") {
    const onVisible = (): void => {
      if (document.visibilityState === "visible") void reconcilePendingElicitations(id);
    };
    document.addEventListener("visibilitychange", onVisible);
    controller.signal.addEventListener("abort", () => {
      document.removeEventListener("visibilitychange", onVisible);
    });
  }

  // Snapshot the session metadata and hydrate the most recent page of
  // item history. The pump may have already pushed blocks by the time
  // this resolves — dedupe by item id.
  // Always refetch the snapshot on bind. A cached session snapshot can
  // be stale after the agent commits new items while the user is viewing
  // another conversation; reusing it drops messages until a page refresh.
  // History is windowed to max(one page, back-to-previous-user-message)
  // so opening a long transcript stays fast while still showing the last
  // full turn and its preceding prompt; `loadMoreHistory` pages older on
  // scroll-up. See `fetchInitialHistoryWindow`.
  // `retry: false` because the most common failure here is "invalid conv
  // id in URL" (not transient).
  if (queryClient === null) {
    throw new Error("chatStore.bindStream: queryClient not initialized");
  }
  try {
    const [session, page] = await Promise.all([
      queryClient.fetchQuery({
        queryKey: ["session", id],
        queryFn: () => getSessionSlim(id, { refreshState: true }),
        staleTime: 0,
        retry: false,
      }),
      fetchInitialHistoryWindow(id),
    ]);
    if (get().conversationId !== id) return;
    const items = page.items;

    // Sticky-pref handoff for CLI-created sessions with no override.
    const nativeModelFamily = nativeModelFamilyForSession(session);
    // Binding-derived fields (isNativeTerminalSession, bound agent,
    // model/skills metadata) — shared with the session.agent_changed
    // refresh path; see sessionBindingPatch.
    const bindingPatch = sessionBindingPatch(session);
    // Sub-agents inherit orchestrator choices.
    const isSubAgentSession = session.parentSessionId != null;
    const canApplyEffort = supportsEffortControl(session);
    const stickyEffort = get().selectedEffort;
    const stickyModel = get().selectedModel;
    // Apply sticky effort only where the Web UI control is meaningful.
    const effectiveEffort = canApplyEffort
      ? (session.reasoningEffort ?? stickyEffort ?? null)
      : stickyEffort;
    // Non-native: don't auto-apply the model, but keep the sticky pick so
    // navigating back to a native session restores it.
    const compatibleStickyModel =
      nativeModelFamily !== null && stickyModel != null
        ? isNativeModelCompatible(nativeModelFamily, stickyModel, session)
          ? stickyModel
          : null
        : stickyModel;
    const effectiveModel =
      nativeModelFamily !== null ? (session.modelOverride ?? compatibleStickyModel) : stickyModel;
    // The session's REAL effective override: the server's stored value,
    // plus the sticky model the native handoff is about to apply. Unlike
    // `effectiveModel`/`selectedModel` (which hold the unapplied sticky
    // pick for non-native sessions), this is the session truth the `/model`
    // readout shows, so a non-applied sticky pick is never mislabeled as
    // an active "(override)".
    const willApplyStickyModel =
      !isSubAgentSession &&
      nativeModelFamily !== null &&
      session.modelOverride == null &&
      compatibleStickyModel != null;
    const effectiveSessionOverride =
      session.modelOverride ?? (willApplyStickyModel ? compatibleStickyModel : null);
    if (
      !isSubAgentSession &&
      canApplyEffort &&
      session.reasoningEffort == null &&
      stickyEffort != null
    ) {
      updateSession(id, { reasoningEffort: stickyEffort }).catch((err: unknown) => {
        console.warn(`Failed to apply sticky effort=${stickyEffort} to session ${id}:`, err);
      });
    }
    if (willApplyStickyModel) {
      updateSession(id, { modelOverride: compatibleStickyModel, silent: true }).catch(
        (err: unknown) => {
          console.warn(`Failed to apply sticky model=${compatibleStickyModel} to session ${id}:`, err);
        },
      );
    }

    const snapshotBlocks = itemsToBlocks(items);
    // Replay outstanding elicitation prompts from the snapshot.
    // The live SSE stream has no buffer, so a prompt that fired
    // before this chat was opened wouldn't render otherwise.
    const pendingElicitationBlocks = pendingElicitationBlocksFromSnapshot(session);
    const oldestItemId = items[0]?.id ?? null;
    set((state) => {
      const seenItemIds = new Set(
        state.blocks.map((b) => b.ctx.itemId).filter((iid): iid is string => Boolean(iid)),
      );
      const unique = snapshotBlocks.filter((b) => !b.ctx.itemId || !seenItemIds.has(b.ctx.itemId));
      // Dedupe against any elicitation blocks already produced by
      // the live pump (the snapshot may race ahead of or behind
      // the SSE event — match by elicitationId).
      const seenElicitationIds = new Set(
        state.blocks
          .filter((b): b is typeof b & { type: "elicitation" } => b.type === "elicitation")
          .map((b) => b.elicitationId),
      );
      const uniquePendingElicitations = pendingElicitationBlocks.filter(
        (b) => b.type !== "elicitation" || !seenElicitationIds.has(b.elicitationId),
      );
      // Synthesize a visible error block when the session failed and no error
      // block was already produced by itemsToBlocks. The `response.error` SSE
      // event is transient (published to the in-memory session stream which
      // has no replay), so clients that connect after the task has already
      // failed never receive it. `last_task_error` on the snapshot is the
      // durable equivalent — use it to ensure the failure reason is always
      // visible on historical load.
      // Pending elicitations land after the historical blocks (and any
      // live blocks the pump already inserted) so the ApprovalCard
      // appears at the bottom of the chat — same position the live
      // stream would have given it.
      const allBlocks = [...unique, ...state.blocks, ...uniquePendingElicitations];
      const hasErrorBlock = allBlocks.some((b) => b.type === "error");
      // Decide the optimistic user bubbles to render after this bind, and
      // (on cold load) keep the per-conversation stash consistent.
      //
      // Rebind (``hydratePending=false``): the live ``pendingUserMessages``
      // are authoritative — keep them untouched. Deduping/merging here would
      // flink the live bubble; they clear via the consumed FIFO path.
      //
      // Cold load (``hydratePending=true``): the server's ``pending_inputs``
      // is the source of truth for queued-but-unpersisted messages — replay
      // ALL of it (the viewer's own and collaborators' alike). The only
      // client-side additions are the bubbles ``switchTo`` restored from
      // the stash: own sends whose POST hadn't settled at navigate-away,
      // which the server can't replay because it hasn't been told about
      // them yet. A restored bubble the server turns out to know
      // after all (its record landed while the POST response was still in
      // transit) is dropped in favor of its content-identical
      // ``pending_inputs`` twin: the server entry carries the durable
      // pending id, so the eventual consumed event clears it precisely —
      // keeping both would double-render and strand one of them.
      const toPending = (p: PendingInput): PendingUserMessage => ({
        tempId: p.pendingId,
        content: p.content,
        ...(p.createdBy !== undefined ? { author: p.createdBy } : {}),
      });
      let candidatePending: PendingUserMessage[];
      if (!hydratePending) {
        candidatePending = state.pendingUserMessages;
      } else {
        const serverPending = (session.pendingInputs ?? []).map(toPending);
        // One-to-one consumption so two identical queued sends still match
        // pairwise. Content (not text) equality so image-only messages
        // correlate too.
        const unmatchedServer = serverPending.map((p) => contentKeyOf(p.content));
        const unknownToServer = state.pendingUserMessages.filter((p) => {
          const i = unmatchedServer.indexOf(contentKeyOf(p.content));
          if (i === -1) return true;
          unmatchedServer.splice(i, 1);
          return false;
        });
        // pending_inputs is FIFO-ordered and sends are serialized through
        // the send chain, so server-known entries precede in-flight ones.
        candidatePending = [...serverPending, ...unknownToServer];
      }
      // Dedupe on a COLD LOAD only: drop any candidate whose message already
      // committed — a snapshot-replayed ghost the server never drained, or a
      // restored stash bubble whose message persisted while the user was
      // away. Without this the bubble double-renders beside the committed
      // item. Native has no id to correlate the POST with the mirrored item,
      // so dedupe by text; the transcript prepends markers/blockquotes,
      // leaving the POSTed text at the end, so match with endsWith.
      // Image-only entries (no text) are kept.
      //
      // Baseline-aware so an optimistic bubble whose text coincides with an
      // OLDER message already in history isn't wrongly dropped: a stashed own
      // bubble (``pend_`` id) is deduped only against committed copies that
      // are NEW since it was stashed — i.e. the snapshot now has MORE copies
      // of that text than the stash's committedTexts baseline. Foreign /
      // server-replayed entries (server ids) have no baseline (it stays 0),
      // so they dedupe against all committed copies as before. Without this,
      // re-sending text that already appears in a resumed/disconnected
      // session's history makes the new bubble vanish until it commits (the
      // "disappears then reappears" report).
      const dedupePending = hydratePending && candidatePending.length > 0;
      const committedUserTexts = dedupePending ? committedUserTextsOf(allBlocks) : [];
      const stashBaseline = state.pendingByConversation[id]?.committedTexts ?? [];
      const countEndsWith = (texts: string[], suffix: string): number =>
        texts.reduce((n, c) => (c.endsWith(suffix) ? n + 1 : n), 0);
      const snapshotPending: PendingUserMessage[] = dedupePending
        ? candidatePending.filter((p) => {
            const text = messageContentText(p.content);
            if (text === "") return true;
            const baseline = p.tempId.startsWith("pend_") ? countEndsWith(stashBaseline, text) : 0;
            return countEndsWith(committedUserTexts, text) <= baseline;
          })
        : candidatePending;
      // Keep the stash consistent with what survived dedupe on cold load: a
      // restored bubble whose message committed while away is now dropped, so
      // prune it from the stash too (else it lingers until the next
      // switch-away). Only this client's own bubbles (``pend_`` ids) belong
      // in the stash — foreign entries are re-served by pending_inputs. Reset
      // the baseline to the now-current committed texts so a subsequent
      // navigate-away/back compares against history as it stands after this
      // bind.
      const prunedStash = hydratePending
        ? (() => {
            const own = snapshotPending.filter((p) => p.tempId.startsWith("pend_"));
            const next = { ...state.pendingByConversation };
            if (own.length > 0) next[id] = { messages: own, committedTexts: committedUserTexts };
            else delete next[id];
            return next;
          })()
        : state.pendingByConversation;
      const syntheticError: ErrorBlock | null =
        session.status === "failed" && session.lastTaskError != null && !hasErrorBlock
          ? {
              type: "error",
              ctx: { agent: null, depth: 0, turn: 0, timestamp: 0, responseId: "", itemId: null },
              message: session.lastTaskError.message,
              source: "",
              code: session.lastTaskError.code,
            }
          : null;
      return {
        ...bindingPatch,
        blocks: syntheticError !== null ? [...allBlocks, syntheticError] : allBlocks,
        pendingUserMessages: snapshotPending,
        pendingByConversation: prunedStash,
        loadingConversation: false,
        hasMoreHistory: page.hasMore,
        oldestItemId,
        // The window cursor was reset: void any in-flight loadMoreHistory.
        historyGeneration: state.historyGeneration + 1,
        // The voided page's stale early-return skips its own flag clear.
        loadingMoreHistory: false,
        sessionStatus: session.status,
        selectedEffort: effectiveEffort,
        selectedModel: effectiveModel,
        // Session truth for the `/model` readout — overrides the snapshot
        // value spread via `...bindingPatch` so the claude-native sticky
        // handoff (fired above, silent) shows immediately.
        sessionModelOverride: effectiveSessionOverride,
        tokensUsed: session.lastTotalTokens ?? null,
        sessionCostUsd: session.totalCostUsd ?? null,
        sessionUsageByModel: session.usageByModel ?? null,
        todos: (session.todos ?? []) as Array<{
          content: string;
          status: "pending" | "in_progress" | "completed";
          activeForm: string;
        }>,
      };
    });
  } catch (err) {
    if (get().conversationId !== id) return;
    set({
      loadingConversation: false,
      conversationLoadError: err instanceof Error ? err : new Error(String(err)),
    });
  }
}

/**
 * Resolve after `ms`, or immediately when `signal` aborts (so switchTo /
 * unmount interrupts a pending reconnect backoff instead of stalling the
 * loop's teardown).
 */
function abortableDelay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const onAbort = (): void => {
      clearTimeout(timer);
      signal.removeEventListener("abort", onAbort);
      resolve();
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal.addEventListener("abort", onAbort, { once: true });
    // Register first, then re-check: an abort that fired before the listener
    // was attached won't dispatch to it, so resolve now if already aborted.
    // (`resolve` is idempotent; this closes any registration-ordering gap.)
    if (signal.aborted) onAbort();
  });
}

/**
 * Halved-to-full jittered exponential backoff between CONSECUTIVE failed
 * opens. Only called with `failedOpens >= 1` — a drop after a healthy
 * connection reconnects instantly (no delay), so the first attempt
 * (`failedOpens === 1`) backs off from the base, doubling per failure up
 * to the cap.
 */
function nextReconnectDelay(failedOpens: number): number {
  const base = Math.min(STREAM_RECONNECT_BASE_MS * 2 ** (failedOpens - 1), STREAM_RECONNECT_MAX_MS);
  return base / 2 + Math.random() * (base / 2);
}

/**
 * Drop the ephemeral (un-persisted) streamed blocks ahead of a reconnect.
 *
 * The server replays an in-flight turn's assistant TEXT on reconnect
 * but NOT its committed items — tool calls / completed messages were
 * persisted mid-turn and keep their `itemId`s. Two replay shapes, two
 * drops:
 *
 * - Response-scoped (in-process agents): the replay is a fresh
 *   `response.created` + the joined streamed-so-far text under the same
 *   `responseId`, so the in-flight response's `itemId`-less blocks (its
 *   `response_start` marker and streamed text/reasoning chunks) are
 *   dropped; the replay rebuilds them exactly once. Gated on a
 *   streaming `activeResponse` — without one there is no rid to scope
 *   the drop.
 * - Native live previews (`live:<message_id>` provisional blocks): the
 *   replay is one CUMULATIVE delta per in-flight message (the joined
 *   text so far at its highest index), and the fresh pump has no
 *   high-water index for the old preview — appending the replay to a
 *   surviving copy would double the text. A message that committed
 *   during the gap is excluded from the replay entirely; its preview
 *   must vanish too, or it would double-render beside the committed
 *   item the reconnect backfill splices in. So previews are dropped
 *   unconditionally (NOT gated on `activeResponse` — native sessions
 *   stream mid-turn while `session.status`-driven, e.g. parked on a
 *   permission prompt).
 *
 * Committed blocks are kept in place so they aren't lost (the replay
 * won't resend them) and dedupe by `itemId` against the live tail.
 * Elicitation and error blocks are itemId-less but NOT part of the
 * text replay (they are never items, and the SSE stream has no
 * elicitation replay), so they're kept too — dropping a pending
 * ApprovalCard here would orphan the parked prompt until a full page
 * refresh.
 */
function dropEphemeralInFlightBlocks(id: string, set: Setter): void {
  set((s) => {
    if (s.conversationId !== id) return {};
    const active = s.activeResponse;
    const rid =
      active !== null && active.state === "streaming" && active.responseId
        ? active.responseId
        : null;
    const kept = s.blocks.filter((b) => {
      if (isLiveProvisionalBlock(b)) return false;
      if (b.type === "elicitation" || b.type === "error") return true;
      return rid === null || b.ctx.responseId !== rid || Boolean(b.ctx.itemId);
    });
    if (kept.length === s.blocks.length) return {};
    return { blocks: kept };
  });
}

/**
 * How many pages `reconcileOnReconnect` will walk backwards looking for
 * overlap with the already-rendered transcript before giving up and
 * re-hydrating the window wholesale. Bounds the per-reconnect fan-out for
 * a very long disconnect gap (the fallback is one initial-window fetch).
 */
const RECONNECT_BACKFILL_MAX_PAGES = 4;

/**
 * Session-snapshot state every reconnect path recovers: `sessionStatus`,
 * token/context/cost counters, and — when the turn ended during the gap —
 * the terminal `activeResponse` transition the missed `session.status`
 * event would have applied, so "Working…" clears.
 */
function reconnectStatusPatch(session: Session, s: ChatState): Partial<ChatState> {
  const patch: Partial<ChatState> = { sessionStatus: session.status };
  if (session.contextWindow != null) patch.contextWindow = session.contextWindow;
  if (session.lastTotalTokens != null) patch.tokensUsed = session.lastTotalTokens;
  if (session.totalCostUsd != null) patch.sessionCostUsd = session.totalCostUsd;
  if (session.usageByModel != null) patch.sessionUsageByModel = session.usageByModel;
  if (
    (session.status === "idle" || session.status === "failed") &&
    s.activeResponse?.state === "streaming"
  ) {
    patch.activeResponse = {
      ...s.activeResponse,
      state: session.status === "failed" ? "failed" : "completed",
      error: null,
    };
    patch.status = "idle";
  }
  return patch;
}

/**
 * Reconcile rendered ApprovalCards against a reconnect snapshot's
 * pending-elicitation list.
 *
 * Elicitations are keyed by `elicitationId`, never `itemId` (they are
 * not persisted items), so the reconnect item backfill can't recover
 * them — and the SSE stream has no elicitation replay, so a prompt
 * whose `response.elicitation_request` fired into the dead socket
 * would otherwise stay invisible until a page refresh. The snapshot's
 * `pending_elicitations` (served from the server's in-memory index) is
 * the source of truth for what is still parked. Three reconciliations:
 *
 * - A prompt in the snapshot with no rendered card → append a fresh
 *   pending card (it fired during the gap).
 * - A rendered pending card absent from the snapshot → flip to
 *   "Resolved elsewhere" (it was answered during the gap), mirroring
 *   the missed `response.elicitation_resolved` event.
 * - A rendered auto-resolved card present in the snapshot → flip back
 *   to pending in place (the prompt re-parked after its deferred clear
 *   fired), so the user can still answer it.
 *
 * The two flips are restricted to cards captured in the `preGap*` sets
 * — cards the caller saw BEFORE fetching the snapshot. Cards the live
 * pump adds or resolves while the fetch is in flight are newer than
 * the snapshot and must not be rewound by its stale view.
 *
 * Returns the patched block list, or `null` when nothing changed.
 */
function reconcileElicitationBlocks(
  blocks: AnyBlock[],
  snapshotPending: AnyBlock[],
  preGapPendingIds: Set<string>,
  preGapAutoResolvedIds: Set<string>,
): AnyBlock[] | null {
  const pendingNow = new Set(
    snapshotPending
      .filter((b): b is ElicitationBlock => b.type === "elicitation")
      .map((b) => b.elicitationId),
  );
  let changed = false;
  const renderedIds = new Set<string>();
  const patched = blocks.map((b) => {
    if (b.type !== "elicitation") return b;
    renderedIds.add(b.elicitationId);
    if (
      b.status === "pending" &&
      preGapPendingIds.has(b.elicitationId) &&
      !pendingNow.has(b.elicitationId)
    ) {
      changed = true;
      const updated: ElicitationBlock = {
        ...b,
        status: "responded",
        response: { action: "auto_resolved" },
      };
      return updated;
    }
    if (
      b.status === "responded" &&
      b.response?.action === "auto_resolved" &&
      preGapAutoResolvedIds.has(b.elicitationId) &&
      pendingNow.has(b.elicitationId)
    ) {
      changed = true;
      const updated: ElicitationBlock = { ...b, status: "pending", response: null };
      return updated;
    }
    return b;
  });
  // Gap-fired prompts land at the bottom of the chat — the same
  // position the live stream would have given them.
  const missing = snapshotPending.filter(
    (b) => b.type === "elicitation" && !renderedIds.has(b.elicitationId),
  );
  if (missing.length === 0 && !changed) return null;
  return [...patched, ...missing];
}

/**
 * Snapshot the ids of currently rendered elicitation cards, split by
 * answerable state, BEFORE a snapshot fetch. `pending` cards are
 * eligible for the gap-resolved flip; `autoResolved` cards are
 * eligible for the re-parked revival. See
 * `reconcileElicitationBlocks` for why eligibility is captured ahead
 * of the fetch.
 */
function captureElicitationIdsByStatus(blocks: AnyBlock[]): {
  pending: Set<string>;
  autoResolved: Set<string>;
} {
  const pending = new Set<string>();
  const autoResolved = new Set<string>();
  for (const b of blocks) {
    if (b.type !== "elicitation") continue;
    if (b.status === "pending") pending.add(b.elicitationId);
    else if (b.response?.action === "auto_resolved") autoResolved.add(b.elicitationId);
  }
  return { pending, autoResolved };
}

/**
 * Reconnect fallback when the disconnect gap outran the incremental
 * backfill cap: replace the history window wholesale from a fresh
 * initial-window fetch, exactly as a cold bind would. Pre-gap blocks are
 * dropped (the fresh window re-covers the newest items; older turns stay
 * reachable via scroll-up, since `oldestItemId` / `hasMoreHistory` are
 * reset alongside) while the live tail the reconnected pump has already
 * delivered — newly committed items plus the active turn's replayed
 * in-flight ephemera — is kept after the window, along with
 * elicitation/error blocks (never items, so the fresh fetch can't
 * recreate them). Elicitation cards are then reconciled against the
 * snapshot's pending list (see `reconcileElicitationBlocks`).
 */
async function rehydrateWindowOnReconnect(
  id: string,
  session: Session,
  preGapIds: Set<string>,
  preGapElicitations: { pending: Set<string>; autoResolved: Set<string> },
  set: Setter,
  get: Getter,
): Promise<void> {
  // Pinned at entry (still the caller's generation — its guards just passed).
  const generation = get().historyGeneration;
  let fresh: SessionItemsPage;
  try {
    fresh = await fetchInitialHistoryWindow(id);
  } catch {
    return;
  }
  if (get().conversationId !== id || get().historyGeneration !== generation) return;
  const freshBlocks = itemsToBlocks(fresh.items);
  const snapshotPending = pendingElicitationBlocksFromSnapshot(session);
  set((s) => {
    const rid = s.activeResponse?.state === "streaming" ? s.activeResponse.responseId : null;
    const tail = s.blocks.filter((b) => {
      if (b.ctx.itemId) return !preGapIds.has(b.ctx.itemId);
      // Elicitation/error blocks aren't items, so the fresh fetch can't recreate them.
      if (b.type === "elicitation" || b.type === "error") return true;
      return rid !== null && b.ctx.responseId === rid;
    });
    const tailIds = new Set(
      tail.map((b) => b.ctx.itemId).filter((iid): iid is string => Boolean(iid)),
    );
    const merged = [
      ...freshBlocks.filter((b) => !b.ctx.itemId || !tailIds.has(b.ctx.itemId)),
      ...tail,
    ];
    return {
      ...reconnectStatusPatch(session, s),
      blocks:
        reconcileElicitationBlocks(
          merged,
          snapshotPending,
          preGapElicitations.pending,
          preGapElicitations.autoResolved,
        ) ?? merged,
      hasMoreHistory: fresh.hasMore,
      oldestItemId: fresh.items[0]?.id ?? null,
      loadingMoreHistory: false,
      // The window cursor was reset: void any in-flight loadMoreHistory.
      historyGeneration: s.historyGeneration + 1,
    };
  });
}

/**
 * Reconcile committed state after a reconnect.
 *
 * Re-fetches the session snapshot + the most-recent items pages and splices
 * in any committed items the live tail can't resupply — items that
 * committed during the disconnect gap, whose stream events fired into a
 * dead socket. Pages backwards (newest-first) until a fetched page overlaps
 * an already-rendered item — or the conversation start — so a gap longer
 * than one page can't leave an unreachable hole between the window and the
 * live tail; if `RECONNECT_BACKFILL_MAX_PAGES` is hit without overlap, the
 * window is re-hydrated wholesale instead (see
 * `rehydrateWindowOnReconnect`). Dedupes by `itemId` and runs concurrently
 * with the live pump — the same race-safe "stream-then-snapshot" shape
 * `bindStream` uses, so a turn that completes between the fetch and the
 * reopen is still caught by one or the other.
 *
 * Also recovers the working-indicator state (`sessionStatus` /
 * `activeResponse`) from the snapshot so a gap-completed turn doesn't leave
 * the spinner stuck, and reconciles ApprovalCards against the snapshot's
 * `pending_elicitations` (see `reconcileElicitationBlocks`) — elicitations
 * are not items, so the item backfill can't recover prompts that fired or
 * resolved while the socket was dead. The backfill path leaves history-window state
 * (`hasMoreHistory` / `oldestItemId`) and sticky picker prefs untouched —
 * a reconnect is not a re-hydrate. Swallows fetch errors: a transient
 * failure just means the next reconnect retries. All writes are
 * `historyGeneration`-guarded so a window reset mid-fetch voids them.
 */
async function reconcileOnReconnect(id: string, set: Setter, get: Getter): Promise<void> {
  if (queryClient === null) return;
  // Captured before any await: the ids rendered BEFORE the gap. The overlap
  // check below must not be satisfied by items the reconnected pump appends
  // while we fetch — those are at the new end of the transcript, not proof
  // the fetched window reaches back to the pre-gap one.
  const preGapIds = new Set(
    get()
      .blocks.map((b) => b.ctx.itemId)
      .filter((iid): iid is string => Boolean(iid)),
  );
  // Same pre-gap capture for elicitation cards: only cards rendered
  // before the snapshot fetch are eligible for its flips — see
  // `reconcileElicitationBlocks`.
  const preGapElicitations = captureElicitationIdsByStatus(get().blocks);
  // A window reset mid-fetch (A→B→A revisit, rebind) defeats the id check alone.
  const generation = get().historyGeneration;
  const stale = (): boolean =>
    get().conversationId !== id || get().historyGeneration !== generation;
  let session: Session;
  let page: SessionItemsPage;
  try {
    [session, page] = await Promise.all([
      queryClient.fetchQuery({
        queryKey: ["session", id],
        queryFn: () => getSessionSlim(id),
        staleTime: 0,
        retry: false,
      }),
      fetchSessionItemsPage(id),
    ]);
  } catch {
    return;
  }
  if (stale()) return;

  // Page backwards until the fetched window reaches the pre-gap transcript
  // or the conversation start. A single newest page is not enough: a gap
  // longer than one page would otherwise leave items no code path can ever
  // fetch (loadMoreHistory only pages older than the pre-gap window top).
  let items = page.items;
  let hasMore = page.hasMore;
  let covered = !hasMore || items.some((it) => preGapIds.has(it.id));
  for (let fetched = 1; !covered && fetched < RECONNECT_BACKFILL_MAX_PAGES; fetched += 1) {
    const cursor = items[0]?.id;
    if (!cursor) break;
    let older: SessionItemsPage;
    try {
      older = await fetchSessionItemsPage(id, { olderThan: cursor });
    } catch {
      return;
    }
    if (stale()) return;
    items = [...older.items, ...items];
    hasMore = older.hasMore;
    covered = !hasMore || older.items.some((it) => preGapIds.has(it.id));
    if (older.items.length === 0) break; // no progress; avoid refetching the same cursor
  }
  if (!covered) {
    await rehydrateWindowOnReconnect(id, session, preGapIds, preGapElicitations, set, get);
    return;
  }

  const snapshotBlocks = itemsToBlocks(items);
  const snapshotPending = pendingElicitationBlocksFromSnapshot(session);
  set((s) => {
    const seen = new Set(
      s.blocks.map((b) => b.ctx.itemId).filter((iid): iid is string => Boolean(iid)),
    );
    const unseen = snapshotBlocks.filter((b) => b.ctx.itemId && !seen.has(b.ctx.itemId));
    const patch: Partial<ChatState> = reconnectStatusPatch(session, s);
    let nextBlocks = s.blocks;
    if (unseen.length > 0) {
      // Splice the gap's committed items ahead of the active turn's
      // replayed in-flight region (its itemId-less blocks, rebuilt by the
      // pump at the tail). With no replay region yet, anchor AFTER the
      // rid's last block: the rid's gap items are newer than its pre-gap
      // committed blocks, so before-its-first would invert the bubble.
      // No rid blocks at all: append; the later replay lands after.
      const rid = s.activeResponse?.state === "streaming" ? s.activeResponse.responseId : null;
      let at = -1;
      if (rid) {
        at = s.blocks.findIndex((b) => b.ctx.responseId === rid && !b.ctx.itemId);
        if (at === -1) {
          const lastRid = s.blocks.findLastIndex((b) => b.ctx.responseId === rid);
          if (lastRid !== -1) at = lastRid + 1;
        }
      }
      nextBlocks =
        at >= 0
          ? [...s.blocks.slice(0, at), ...unseen, ...s.blocks.slice(at)]
          : [...s.blocks, ...unseen];
    }
    // Recover elicitation state the dead socket swallowed: gap-fired
    // prompts, gap-resolved cards, and re-parked prompts whose card
    // was auto-cleared. Items can't resupply these (elicitations are
    // never items), so this is the only path that fixes them short of
    // a full page refresh.
    const reconciled = reconcileElicitationBlocks(
      nextBlocks,
      snapshotPending,
      preGapElicitations.pending,
      preGapElicitations.autoResolved,
    );
    if (reconciled !== null) nextBlocks = reconciled;
    if (nextBlocks !== s.blocks) patch.blocks = nextBlocks;
    return patch;
  });
}

// ── Presence idle reporting ─────────────────────────────────────────
// The stream GET's `idle` query param is the entire client→server
// presence uplink (no dedicated endpoint), so an idle flip is delivered
// by recycling the live stream attempt — the same abort-and-reopen the
// ingress already forces every ~5 minutes. The tracker aborts only the
// per-attempt controller; the outer controller (teardown) stays live.
let presenceAttemptController: AbortController | null = null;
const presenceIdle = createPresenceIdleTracker({
  onFlip: () => presenceAttemptController?.abort(),
});
if (typeof document !== "undefined") {
  document.addEventListener("visibilitychange", () =>
    presenceIdle.handleVisibilityChange(document.hidden),
  );
}

/**
 * Own the session SSE stream for the lifetime of a bound conversation,
 * reconnecting transparently across drops.
 *
 * One connection at a time: open `/stream`, pump it via
 * `pumpStreamEvents`, and on a `"dropped"` end (the Databricks Apps ingress
 * recycling the long-lived HTTP/2 stream at its ~5-min cap, or any
 * connection break) re-subscribe after a jittered backoff. Stops only on
 * intentional teardown (`"aborted"` — switchTo / unmount), a conversation
 * switch (`"switched"`), a deliberate server close (`"server_closed"` —
 * the `[DONE]` sentinel), or a permanent open failure (401/403/404).
 *
 * `abortController` is held across reconnect attempts (so `send`'s
 * `ensureBoundSession` doesn't see a dead binding and rebind redundantly
 * during a transient gap) and cleared only when this loop exits.
 *
 * On a re-connect — but not the first connect, whose snapshot `bindStream`
 * already hydrates — the loop drops the stale in-flight bubble and
 * reconciles the committed snapshot concurrently with the live pump, so
 * the server's replay rebuilds the streaming turn without duplication and
 * a gap-completed turn isn't lost.
 */
export async function startStreamPump(
  id: string,
  controller: AbortController,
  set: Setter,
  get: Getter,
): Promise<void> {
  let failedOpens = 0;
  // True once we've had at least one SUCCESSFUL open. Drives reconnect-only
  // behavior (drop in-flight + reconcile), which must NOT run on the first
  // established stream — failed opens leave it false so a recovered first
  // connect is still treated as initial, not a reconnect.
  let hasConnected = false;
  // A reconnect loop is inherently sequential — open → pump → reconnect —
  // so its awaits cannot be parallelized; no-await-in-loop doesn't apply.
  /* eslint-disable no-await-in-loop */
  try {
    while (!controller.signal.aborted && get().conversationId === id) {
      // Back off only between consecutive failed opens. A drop after a
      // healthy connection (the benign ~5-min ingress recycle) leaves
      // failedOpens at 0, so it reconnects instantly with no delay.
      if (failedOpens > 0) {
        await abortableDelay(nextReconnectDelay(failedOpens), controller.signal);
        if (controller.signal.aborted || get().conversationId !== id) break;
      }

      // Per-attempt controller: a presence idle flip recycles just this
      // connection (the `idle` query param is the entire presence uplink,
      // so the flip must arrive as a reconnect). Outer aborts (switchTo /
      // unmount) forward in so teardown still cancels the live fetch.
      const attempt = new AbortController();
      const onOuterAbort = () => attempt.abort();
      controller.signal.addEventListener("abort", onOuterAbort);
      presenceAttemptController = attempt;
      try {
        const idle = presenceIdle.idleNow();
        let streamRes: Response;
        try {
          streamRes = await openSessionStream(id, attempt.signal, { idle });
        } catch (err) {
          if (err instanceof Error && err.name === "AbortError") {
            if (controller.signal.aborted || get().conversationId !== id) break;
            // Only the attempt was aborted (presence flip mid-open) —
            // reopen immediately with the recomputed idle flag.
            continue;
          }
          if (get().conversationId !== id) break;
          console.warn(`Session ${id}: stream connect failed, will retry`, err);
          failedOpens += 1;
          continue;
        }

        if (controller.signal.aborted || get().conversationId !== id) break;
        if (!streamRes.ok || !streamRes.body) {
          // Release the unconsumed error-response body so the underlying fetch
          // connection is freed promptly rather than lingering across retries.
          void streamRes.body?.cancel().catch(() => {});
          // 401/403/404 won't fix themselves by retrying — give up and mark
          // the session failed so the user isn't left on a silent spinner.
          if (streamRes.status === 401 || streamRes.status === 403 || streamRes.status === 404) {
            console.warn(`Session ${id}: stream unavailable (${streamRes.status}), giving up`);
            finalizeActive(set, "failed", `stream unavailable (${streamRes.status})`, null);
            set({ sessionStatus: "failed", status: "idle" });
            break;
          }
          console.warn(`Session ${id}: stream open failed (${streamRes.status}), will retry`);
          failedOpens += 1;
          continue;
        }

        const reconnecting = hasConnected;
        hasConnected = true;
        failedOpens = 0;
        presenceIdle.noteReported(idle);
        if (reconnecting) {
          dropEphemeralInFlightBlocks(id, set);
        }
        // Start the pump, then reconcile the snapshot concurrently (race-safe
        // via itemId dedup) — mirrors bindStream's stream-then-snapshot order.
        const pumpPromise = pumpStreamEvents(id, streamRes.body, controller, set, get);
        if (reconnecting) {
          await reconcileOnReconnect(id, set, get);
        }
        let reason = await pumpPromise;

        // A presence flip aborts only the attempt; the pump reads that as
        // "aborted" but the outer controller is still live — reconnect so
        // the new idle flag reaches the server.
        if (reason === "aborted" && !controller.signal.aborted) {
          reason = "dropped";
        }
        // Only a transport drop is reconnectable; everything else ends the loop.
        if (reason !== "dropped") break;
      } finally {
        controller.signal.removeEventListener("abort", onOuterAbort);
        if (presenceAttemptController === attempt) {
          presenceAttemptController = null;
        }
      }
    }
  } finally {
    if (get().abortController === controller) {
      set({ abortController: null });
    }
  }
  /* eslint-enable no-await-in-loop */
}

/**
 * Coalesces a frame's worth of work onto a single callback.
 *
 * `schedule` is single-flight — calling it again while a frame is
 * already pending is a no-op, so N appends within one frame collapse
 * to one flush. `cancel` drops the pending frame without firing it.
 */
export interface FrameScheduler {
  schedule: (cb: () => void) => void;
  cancel: () => void;
}

/**
 * Default `FrameScheduler` backed by `requestAnimationFrame`, so block
 * appends paint at most once per browser frame. Falls back to a 0 ms
 * timer where rAF is absent (SSR / non-DOM); each pump owns its own
 * instance so cancelling one stream's frame can't drop another's.
 */
function createRafScheduler(): FrameScheduler {
  const raf: (cb: () => void) => number =
    typeof requestAnimationFrame === "function"
      ? (cb) => requestAnimationFrame(() => cb())
      : (cb) => setTimeout(cb, 0) as unknown as number;
  const caf: (handle: number) => void =
    typeof cancelAnimationFrame === "function"
      ? (handle) => cancelAnimationFrame(handle)
      : (handle) => clearTimeout(handle);
  let handle: number | null = null;
  return {
    schedule(cb) {
      if (handle !== null) return;
      handle = raf(() => {
        handle = null;
        cb();
      });
    },
    cancel() {
      if (handle !== null) {
        caf(handle);
        handle = null;
      }
    },
  };
}

/**
 * Why a single `pumpStreamEvents` connection ended — see that function's
 * `:returns:`. Only `"dropped"` is reconnectable.
 */
export type StreamEndReason = "aborted" | "switched" | "server_closed" | "dropped";

/**
 * Drive the session SSE stream → BlockStream reducer → state.blocks.
 *
 * Runs for the lifetime of the bound session. Exits on AbortError
 * (when switchTo aborts the controller to bind a different session).
 * Stream-delivered blocks plain-append; the renderer derives ordering
 * from their position in `blocks` plus any trailing entries in
 * `pendingUserMessages`.
 *
 * Batching: reducer-emitted blocks are buffered and flushed in a single
 * `set` per animation frame (`scheduler`), so a fast token stream that
 * emits dozens of `text_chunk` blocks per frame triggers one React
 * commit instead of dozens. The first content block of each response
 * flushes synchronously so first-token paint isn't delayed by a frame,
 * and the buffer is force-flushed before `response_end` side effects so
 * the terminal bubble state is never a frame behind. A pending frame is
 * cancelled (and its buffer dropped) when the pump unwinds — switchTo /
 * abort — so a queued flush can't apply this stream's blocks onto a
 * different session.
 *
 * Dedupe: per-block itemId guard against snapshot collisions, checked
 * against both committed `blocks` and the not-yet-flushed buffer via
 * `seenItemIds`. Empty `itemId` means "no canonical id yet" (e.g.
 * text/reasoning chunks) and bypasses the dedupe.
 *
 * :param scheduler: frame batcher. Defaults to a rAF-backed scheduler;
 *     tests inject a manual one to fire flushes deterministically.
 * :returns: Why the connection ended, so the reconnect loop in
 *     `startStreamPump` can decide whether to re-subscribe:
 *     ``"aborted"`` (switchTo / unmount), ``"switched"`` (conversation
 *     changed mid-pump), ``"server_closed"`` (the server's ``[DONE]``
 *     sentinel — a deliberate close, don't reconnect), or ``"dropped"``
 *     (the byte stream ended or threw without ``[DONE]`` — a transport
 *     drop such as the Databricks Apps ~5-min stream cap, reconnect).
 *     This function deliberately does NOT mark the session failed or
 *     clear `abortController`; the loop owns lifecycle so a transient
 *     drop doesn't flash a failure or trigger a redundant rebind.
 */
// Item-id prefix marking a provisional, in-flight assistant-text block —
// a live-streaming preview that lives in `blocks` until its authoritative
// `text_done` replaces it. Never a real server item id.
const LIVE_ITEM_PREFIX = "live:";

/** Whether a block is a provisional live-streaming text preview. */
function isLiveProvisionalBlock(b: AnyBlock): boolean {
  return b.ctx.itemId?.startsWith(LIVE_ITEM_PREFIX) ?? false;
}

/**
 * Build a provisional in-flight assistant-text block for live streaming.
 *
 * Shaped like a finalized `text_done` so the existing renderer draws it
 * as assistant text, but keyed with a synthetic `live:<messageId>` id —
 * its own `responseId` too, so it forms its own bubble until the
 * authoritative item replaces it; that id is never matched against a
 * real server response.
 *
 * :param itemId: the provisional id, e.g. ``"live:2ca51d97-..."``.
 * :param text: the text accumulated so far, e.g. ``"Hello"``.
 * :returns: a `TextDone` block ready to push into `blocks`.
 */
function makeLiveTextBlock(itemId: string, text: string): TextDone {
  return {
    type: "text_done",
    // ``timestamp`` matches the reducer's monotonic source (not wall
    // clock); it is an ordering hint, not a displayed date.
    ctx: {
      agent: null,
      depth: 0,
      turn: 0,
      timestamp: performance.now() / 1000,
      responseId: itemId,
      itemId,
    },
    fullText: text,
    hasCodeBlocks: text.includes("```"),
  };
}

/**
 * Fold one streamed chunk into its in-flight preview block in `blocks`.
 *
 * The streamed text lives in `blocks` (not a separate lane) as a
 * provisional `text_done` block keyed `live:<messageId>`, inserted at the
 * position the first chunk arrived. Keeping it in `blocks` means a later
 * committed block (a tool card, an elicitation) renders BELOW it in
 * arrival order — see `walkBubbles`. When the authoritative `text_done`
 * arrives it replaces this block in place (`pumpStreamEvents`),
 * preserving that position.
 *
 * Chunks for a message arrive in `index` order (the forwarder tails the
 * deltas file sequentially and dedupes by `(message_id, index)`), so each
 * new chunk's text is appended; a chunk at or below the high-water index
 * is ignored, making a duplicate/replayed chunk a no-op.
 *
 * :param set: store setter.
 * :param messageId: vendor's stable per-message id.
 * :param index: 0-based chunk order within the message.
 * :param delta: incremental text for this chunk, e.g. ``"Hello "``.
 * :param lastIndex: per-message high-water index, mutated in place.
 * :returns: nothing; mutates `blocks` in the store.
 */
function applyLiveDelta(
  set: Setter,
  messageId: string,
  index: number,
  delta: string,
  lastIndex: Map<string, number>,
): void {
  const prev = lastIndex.get(messageId);
  if (prev !== undefined && index <= prev) return;
  lastIndex.set(messageId, index);
  const itemId = LIVE_ITEM_PREFIX + messageId;
  set((s) => {
    const at = s.blocks.findIndex((b) => b.ctx.itemId === itemId);
    if (at === -1) {
      return { blocks: [...s.blocks, makeLiveTextBlock(itemId, delta)] };
    }
    const existing = s.blocks[at]!;
    if (existing.type !== "text_done") return {};
    const fullText = existing.fullText + delta;
    const next = s.blocks.slice();
    next[at] = { ...existing, fullText, hasCodeBlocks: fullText.includes("```") };
    return { blocks: next };
  });
}

/**
 * Wrap a parsed event stream, diverting terminal-observed live deltas.
 *
 * A `text_delta` carrying a `messageId` is claude-native live streaming:
 * it is folded into its provisional preview block in `blocks` (see
 * `applyLiveDelta`) and NOT yielded downstream, because the `BlockStream`
 * reducer's response-scoped text path would otherwise emit a stray bubble
 * (these deltas carry no response id and their authoritative text arrives
 * as a separate committed item). Every other event passes through
 * untouched.
 *
 * Deltas whose `messageId` has been retired (its preview already
 * superseded by the authoritative `text_done`) are dropped: a message's
 * trailing chunk can arrive just after its done event, and replaying it
 * would re-create a finalized message's preview as a duplicate, stale
 * bubble. See the `text_done` branch of `pumpStreamEvents`.
 *
 * :param events: upstream parsed events (already session-tapped).
 * :param id: the conversation this pump is bound to; a late delta from a
 *     switched-away stream is dropped rather than mutating state.
 * :param retired: message ids whose preview has been finalized; their
 *     late deltas are ignored. Shared with the pump loop, which adds to
 *     it when it replaces a preview.
 * :param lastIndex: per-message high-water chunk index, shared with
 *     `applyLiveDelta` for duplicate suppression.
 * :param set: store setter.
 * :param get: store getter.
 * :returns: events with native live deltas removed.
 */
async function* tapLiveDeltas(
  events: AsyncIterable<StreamEvent>,
  id: string,
  retired: Set<string>,
  lastIndex: Map<string, number>,
  set: Setter,
  get: Getter,
): AsyncIterable<StreamEvent> {
  for await (const ev of events) {
    if (ev.type === "text_delta" && ev.messageId !== undefined) {
      if (get().conversationId === id && !retired.has(ev.messageId)) {
        applyLiveDelta(set, ev.messageId, ev.index ?? 0, ev.delta, lastIndex);
      }
      continue;
    }
    yield ev;
  }
}

/**
 * Flip an auto-resolved ApprovalCard back to answerable, in place.
 *
 * Used when the server re-publishes `response.elicitation_request` for
 * an id whose card was already flipped to "Resolved elsewhere" (the
 * deferred clear after a severed harness wait fired before the retry
 * re-parked) — the re-publish proves the prompt is parked again and
 * still waiting for a verdict. Only an `auto_resolved` card is
 * revived: a card carrying a real user verdict means an answer is
 * already in flight, and `submitApproval` owns rolling that back if
 * its POST fails.
 */
function revivePendingElicitationBlock(set: Setter, elicitationId: string): void {
  set((s) => {
    const idx = s.blocks.findIndex(
      (b) => b.type === "elicitation" && b.elicitationId === elicitationId,
    );
    if (idx === -1) return {};
    const target = s.blocks[idx] as ElicitationBlock;
    if (target.status !== "responded" || target.response?.action !== "auto_resolved") return {};
    const updated: ElicitationBlock = { ...target, status: "pending", response: null };
    return { blocks: [...s.blocks.slice(0, idx), updated, ...s.blocks.slice(idx + 1)] };
  });
}

export async function pumpStreamEvents(
  id: string,
  body: ReadableStream<Uint8Array>,
  controller: AbortController,
  set: Setter,
  get: Getter,
  scheduler: FrameScheduler = createRafScheduler(),
): Promise<StreamEndReason> {
  const stream = new BlockStream();
  const sseResult: SseStreamResult = { sawDone: false };
  const rawEvents = parseSseStream(body, sseResult);
  // Tap the raw event stream for `session.*` side effects (sessionStatus,
  // pending-message promotion, interrupted decoration) before handing it
  // to the BlockStream reducer. The reducer is intentionally pure
  // (block factory) — session-scoped state lives on the store, not in
  // the reducer's internal state. See migration plan §5.3.
  // Message ids whose live preview has been finalized by its
  // authoritative `text_done`. Lives for the whole connection (a new
  // session rebinds a fresh pump) so a message's trailing chunk that
  // arrives after its done event can't re-create the preview.
  const retiredLiveMessages = new Set<string>();
  // Per-message high-water chunk index, for delta duplicate suppression.
  const liveLastIndex = new Map<string, number>();
  const events = tapLiveDeltas(
    tapSessionEvents(rawEvents),
    id,
    retiredLiveMessages,
    liveLastIndex,
    set,
    get,
  );

  // Blocks awaiting their coalesced flush; `seenItemIds` dedupes against
  // both committed and still-buffered blocks. Lives for the whole stream
  // (one SSE connection); bounded by item count like `blocks` itself.
  const buffer: AnyBlock[] = [];
  const seenItemIds = new Set<string>();
  // First content block of each response flushes synchronously (snappy
  // first-token paint); the rest batch.
  let paintedFirstContent = false;

  // Drain the buffer (+ optional trailing block) into one `blocks` append,
  // applying any sidecar state in the same commit. No-ops if switched away.
  const flush = (trailing?: AnyBlock, extra?: Partial<ChatState>): void => {
    scheduler.cancel();
    if (get().conversationId !== id) {
      buffer.length = 0;
      return;
    }
    const batch = trailing !== undefined ? [...buffer, trailing] : [...buffer];
    buffer.length = 0;
    if (batch.length === 0) {
      if (extra !== undefined) set(extra);
      return;
    }
    set((s) => {
      // Re-check itemIds at commit time: a snapshot merge can insert an
      // item while it sits in this buffer (merges read only state.blocks),
      // and appending the buffered copy would double-render it. ItemId-less
      // blocks skip the check, so pure token batches stay cheap.
      let fresh = batch;
      if (batch.some((b) => b.ctx.itemId)) {
        const committed = new Set(
          s.blocks.map((b) => b.ctx.itemId).filter((iid): iid is string => Boolean(iid)),
        );
        fresh = batch.filter((b) => !b.ctx.itemId || !committed.has(b.ctx.itemId));
      }
      // Same commit-time recheck for elicitations, keyed by
      // elicitationId: the reconnect reconcile can append the
      // snapshot's copy of a prompt while the live block sits in this
      // buffer.
      if (fresh.some((b) => b.type === "elicitation")) {
        const committedElicitations = new Set(
          s.blocks
            .filter((b): b is ElicitationBlock => b.type === "elicitation")
            .map((b) => b.elicitationId),
        );
        fresh = fresh.filter(
          (b) => b.type !== "elicitation" || !committedElicitations.has(b.elicitationId),
        );
      }
      if (fresh.length === 0) return extra ?? {};
      return { ...(extra ?? {}), blocks: [...s.blocks, ...fresh] };
    });
  };

  try {
    for await (const block of stream.reduce(events)) {
      if (controller.signal.aborted) return "aborted";
      if (get().conversationId !== id) return "switched";

      if (block.type === "response_start") {
        // New response: force-flush whatever is buffered, then land the
        // marker + lifecycle in one commit. Reset the first-paint latch.
        paintedFirstContent = false;
        flush(block, {
          activeResponse: { responseId: block.responseId, state: "streaming", error: null },
          status: "streaming",
        });
        continue;
      }

      // Stream → snapshot dedup: skip if this itemId is already committed
      // or sitting unflushed in the buffer, so the renderer sees one copy.
      if (block.ctx.itemId) {
        if (seenItemIds.has(block.ctx.itemId)) continue;
        if (get().blocks.some((b) => b.ctx.itemId === block.ctx.itemId)) continue;
        seenItemIds.add(block.ctx.itemId);
      }

      // Elicitations are keyed by elicitationId, never itemId (they are
      // not persisted items), so the dedupe above can't see them. The
      // server re-publishes the same id whenever a severed harness wait
      // re-parks (hook retries reuse their id), so an already-rendered
      // id must not append a second card: a pending card (or one
      // carrying an in-flight user verdict) stays untouched, and a card
      // the deferred clear flipped to "Resolved elsewhere" is revived
      // to answerable in place.
      if (block.type === "elicitation") {
        const eid = block.elicitationId;
        if (buffer.some((b) => b.type === "elicitation" && b.elicitationId === eid)) continue;
        if (get().blocks.some((b) => b.type === "elicitation" && b.elicitationId === eid)) {
          revivePendingElicitationBlock(set, eid);
          continue;
        }
      }

      if (block.type === "text_done" && block.ctx.itemId && !isLiveProvisionalBlock(block)) {
        // A persisted assistant message whose text already streamed
        // id-less this response. The relay publishes each flushed text
        // segment as `output_item.done` so clients learn its
        // store-assigned id (see `_flush_relay_text`), but by the time
        // it arrives a tool call / reasoning section has usually closed
        // the streamed text — the reducer's open-section dedupe can't
        // catch it and emits this block as a fresh copy. Stamp the id
        // onto the already-streamed `text_done` IN PLACE instead of
        // appending: the live view keeps one copy in its streamed
        // position (above the tool call), and reconnect reconciliation
        // (itemId-keyed) sees the persisted item as already rendered.
        // FIFO (findIndex): the relay flushes segments in order, so the
        // first unstamped match is the one this item persisted.
        const itemId = block.ctx.itemId;
        const matchesStreamed = (b: AnyBlock): b is TextDone =>
          b.type === "text_done" &&
          !b.ctx.itemId &&
          b.ctx.responseId === block.ctx.responseId &&
          b.fullText === block.fullText;
        const bufferAt = buffer.findIndex(matchesStreamed);
        if (bufferAt !== -1) {
          const streamed = buffer[bufferAt] as TextDone;
          buffer[bufferAt] = { ...streamed, ctx: { ...streamed.ctx, itemId } };
          continue;
        }
        if (get().blocks.some(matchesStreamed)) {
          // Commit buffered blocks first so the stamp lands on the same
          // ordering the user is looking at.
          flush();
          set((s) => {
            const at = s.blocks.findIndex(matchesStreamed);
            if (at === -1) return {};
            const streamed = s.blocks[at]!;
            const next = s.blocks.slice();
            next[at] = { ...streamed, ctx: { ...streamed.ctx, itemId } };
            return { blocks: next };
          });
          continue;
        }
        // No streamed copy to stamp (e.g. a non-streamed message):
        // fall through and append as a normal block.
      }

      if (block.type === "text_done" && get().isNativeTerminalSession) {
        const provIdx = get().blocks.findIndex(isLiveProvisionalBlock);
        if (provIdx !== -1) {
          // The authoritative final text for the oldest in-flight message
          // just arrived. Replace that provisional preview IN PLACE so the
          // committed text keeps the position it streamed into — above any
          // tool/elicitation card that arrived after it. FIFO: claude-
          // native finishes one message before the next begins, and
          // `message_id` is absent from the transcript, so the oldest open
          // preview is the one this item finalizes. Retire its id so a
          // trailing chunk arriving after this event can't re-create it.
          const provItemId = get().blocks[provIdx]!.ctx.itemId!;
          retiredLiveMessages.add(provItemId.slice(LIVE_ITEM_PREFIX.length));
          // Commit any buffered reducer blocks first (preserve their order),
          // then splice the authoritative text into the preview's slot.
          flush();
          set((s) => {
            const at = s.blocks.findIndex(isLiveProvisionalBlock);
            if (at === -1) return { blocks: [...s.blocks, block] };
            const next = s.blocks.slice();
            next[at] = block;
            return { blocks: next };
          });
          paintedFirstContent = true;
          continue;
        }
      }

      if (block.type === "response_end") {
        // Force-flush buffer + marker before the terminal side effects so
        // the bubble's final content commits with its lifecycle transition.
        flush(block);
        // If the active response was already marked cancelled by an
        // earlier `session.interrupted`, keep that. Session events
        // are the authoritative source for user-initiated terminals.
        if (get().activeResponse?.state !== "cancelled") {
          const errorMsg = block.response?.error?.message ?? null;
          finalizeActive(set, block.status as ActiveResponse["state"], errorMsg);
        }
        // Turn over: drop any provisional preview never finalized by a
        // committed item (e.g. an interrupt where the partial item lands
        // after this event, or a stream drop). Normal messages already
        // had their preview replaced when their `text_done` committed, so
        // this is usually a no-op.
        set((s) => ({
          status: "idle",
          blocks: s.blocks.some(isLiveProvisionalBlock)
            ? s.blocks.filter((b) => !isLiveProvisionalBlock(b))
            : s.blocks,
        }));
        const convId = get().conversationId;
        if (convId) {
          queryClient?.invalidateQueries({ queryKey: ["conversation", convId, "items"] });
          // No terminals invalidation: the list is SSE-sourced (see
          // useTerminals). Its query has only an empty seed queryFn, so
          // invalidating would refetch [] and wipe the live list. The
          // session.resource.{created,deleted} deltas already keep it
          // fresh during the turn, and snapshot-on-connect re-seeds it
          // on reconnect.
        }
        continue;
      }

      buffer.push(block);
      if (!paintedFirstContent) {
        // First content of the response — paint it immediately so the
        // user sees the first token without waiting a frame.
        paintedFirstContent = true;
        flush();
      } else {
        scheduler.schedule(() => flush());
      }
    }
    // The byte stream ended. Commit the buffered tail before `finally`
    // clears it, so trailing tokens aren't lost (no-op after a normal
    // `response_end`, which already drained the buffer). Whether this was
    // a deliberate server close (`[DONE]`) or a transport drop without it
    // (idle proxy disconnect / the Apps ~5-min cap) decides reconnection.
    flush();
    return sseResult.sawDone ? "server_closed" : "dropped";
  } catch (err) {
    if (err instanceof Error && err.name === "AbortError") return "aborted";
    if (get().conversationId !== id) return "switched";
    // A reader/parse error (e.g. net::ERR_HTTP2_PROTOCOL_ERROR from the
    // ingress resetting the stream). Commit the tail and report a drop;
    // the reconnect loop re-subscribes rather than marking the turn
    // failed, so a routine recycle stays invisible.
    flush();
    return "dropped";
  } finally {
    // Drop any pending frame + its buffered blocks so a queued flush
    // can't apply this stream's blocks after switchTo bound another.
    // `abortController` lifecycle is owned by `startStreamPump`'s loop,
    // not here — it must survive across reconnect attempts.
    scheduler.cancel();
    buffer.length = 0;
  }
}

/**
 * Extract a typed `MessageContentBlock[]` from a cross-client
 * `session.input.consumed` event whose payload is a user message.
 * Returns `null` if the event does not describe a user message.
 */
function userContentFromEvent(event: SessionInputConsumedEvent): MessageContentBlock[] | null {
  if (event.isMeta === true) return null;
  if (event.itemType !== "message") return null;
  if (event.data.role !== "user") return null;
  const raw = event.data.content;
  if (!Array.isArray(raw)) return null;
  return raw.filter(
    (b): b is MessageContentBlock =>
      typeof b === "object" &&
      b !== null &&
      "type" in b &&
      (b.type === "input_text" || b.type === "input_image" || b.type === "input_file"),
  );
}

function hasCommittedItem(blocks: AnyBlock[], itemId: string): boolean {
  return itemId !== "" && blocks.some((block) => block.ctx.itemId === itemId);
}

/**
 * Build the committed user-message content from a consumed event,
 * preserving optimistic file blocks the native transcript drops.
 *
 * Native sessions' `session.input.consumed` round-trips through the
 * transcript forwarder, which carries only text — no input_image /
 * input_file. When the server content has no file blocks, prepend the
 * ones from the matched optimistic bubble so the thumbnail stays
 * visible. Falls back to the pending content when the event carries no
 * user-message payload at all.
 *
 * @param event - The consumed event.
 * @param pendingContent - Content of the optimistic bubble being promoted, or null when none matched.
 * @returns The content to commit, or null when neither source has content.
 */
function committedContentFor(
  event: SessionInputConsumedEvent,
  pendingContent: MessageContentBlock[] | null,
): MessageContentBlock[] | null {
  const serverContent = userContentFromEvent(event);
  if (!serverContent) return pendingContent;
  if (!pendingContent) return serverContent;
  const serverHasFiles = serverContent.some(
    (b) => b.type === "input_image" || b.type === "input_file",
  );
  if (serverHasFiles) return serverContent;
  const pendingFiles = pendingContent.filter(
    (b) => b.type === "input_image" || b.type === "input_file",
  );
  return [...pendingFiles, ...serverContent];
}

/**
 * A committed user-message block carrying the server item id.
 *
 * @param itemId - Server-assigned conversation item id (for dedup + nav).
 * @param content - The committed message content.
 * @param stableKey - The optimistic bubble's temp id when this block is
 *   promoted from one, so the rendered bubble keeps the same React key
 *   across the swap (no remount/flink). Omit for foreign/TUI messages
 *   that had no optimistic predecessor — they mount fresh.
 */
function committedUserBlock(
  itemId: string,
  content: MessageContentBlock[],
  stableKey?: string,
  createdBy?: string,
): UserMessageBlock {
  return {
    type: "user_message",
    ctx: {
      agent: null,
      depth: 0,
      turn: 0,
      timestamp: 0,
      responseId: "",
      itemId,
      // Live human-author attribution (multi-user); omit when absent so
      // null carries no author. Mirrors itemsToBlocks on cold load.
      ...(createdBy !== undefined ? { createdBy } : {}),
    },
    content,
    stableKey,
  };
}

interface RefetchRunnerBackedSessionStateOptions {
  /** Force the AP server to re-read runner-backed caches before returning. */
  refreshState?: boolean;
  /** Apply the broader binding metadata patch in addition to capabilities. */
  applyBindingPatch?: boolean;
}

/**
 * Refetch runner-backed session state and apply it to the store.
 *
 * Skills and codex-native model options are runner-owned. When a session
 * binds before those background fetches land, the snapshot carries empty
 * lists. The server later sends a bare nudge; refetching the snapshot is
 * how the store pulls the cache-warmed fields without clobbering live chat
 * state. Runner-online refreshes can also ask the server to pierce stale
 * caches and then re-apply binding metadata that is safe to update out of
 * band (agent labels, harness, terminal-pending, sandbox state).
 *
 * Best-effort and race-guarded: a failed fetch (runner dropped again
 * mid-flight) leaves existing state in place, and a result for a
 * conversation the user has since switched away from is dropped.
 *
 * :param conversationId: The session to refetch, e.g. ``"conv_abc123"``.
 * :param options: Whether to force a runner-backed refresh and apply the
 *     broader binding patch.
 */
async function refetchRunnerBackedSessionState(
  conversationId: string,
  options: RefetchRunnerBackedSessionStateOptions = {},
): Promise<void> {
  if (useChatStore.getState().conversationId !== conversationId) return;
  let session: Session;
  try {
    if (queryClient !== null) {
      session = await queryClient.fetchQuery({
        queryKey: ["session", conversationId],
        queryFn: () => getSessionSlim(conversationId, { refreshState: options.refreshState }),
        staleTime: 0,
        retry: false,
      });
    } else {
      session = await getSessionSlim(conversationId, { refreshState: options.refreshState });
    }
  } catch {
    // The runner may have dropped again before the fetch landed. Keep
    // the existing state rather than wiping it on a transient error.
    return;
  }
  // Re-check after the await — the user may have switched conversations
  // while the request was in flight; applying now would leak another
  // session's capabilities into the open composer.
  if (useChatStore.getState().conversationId !== conversationId) return;
  useChatStore.setState(
    options.applyBindingPatch === true
      ? sessionBindingPatch(session)
      : {
          skills: session.skills ?? [],
          codexModelOptions: session.codexModelOptions ?? [],
        },
  );
}

/**
 * Normalized plain text of a user message's content blocks.
 *
 * Joins the text blocks (input_text / output_text) and collapses
 * whitespace so a transcript round-trip that reflows spacing still
 * matches the originally-POSTed text. Returns "" when the content
 * carries no text (e.g. an image-only message); callers treat that as
 * "can't dedupe by text".
 */
function messageContentText(content: MessageContentBlock[]): string {
  return content
    .map((b) => (b.type === "input_text" || b.type === "output_text" ? b.text : ""))
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * Normalized texts of the committed user-message blocks in `blocks`,
 * dropping empties (image-only messages). The dedup baseline for the
 * per-conversation pending stash (see {@link StashedPending.committedTexts}).
 */
function committedUserTextsOf(blocks: AnyBlock[]): string[] {
  return blocks
    .filter((b): b is UserMessageBlock => b.type === "user_message")
    .map((b) => messageContentText(b.content))
    .filter((text) => text.length > 0);
}

/**
 * Canonical JSON key for message content blocks, for correlating a
 * restored in-flight optimistic bubble with a server `pending_inputs`
 * entry by content equality. The server stores the POSTed blocks
 * verbatim, so an exact structural match means "same message" — keys
 * are sorted so serialization order can't break it. Unlike the
 * text-based dedupe this also correlates image-only messages, whose
 * empty text is otherwise unmatchable.
 */
function contentKeyOf(content: MessageContentBlock[]): string {
  const canonical = (v: unknown): unknown => {
    if (Array.isArray(v)) return v.map(canonical);
    if (v !== null && typeof v === "object") {
      const rec = v as Record<string, unknown>;
      return Object.fromEntries(
        Object.keys(rec)
          .sort()
          .map((k) => [k, canonical(rec[k])]),
      );
    }
    return v;
  };
  return JSON.stringify(canonical(content));
}

/**
 * Drop one optimistic entry from a conversation's navigation stash.
 *
 * Called when that entry's POST settles — accepted, denied, or failed.
 * From that point the server owns the message's fate (accepted native:
 * replayed via `pending_inputs` until committed; accepted non-native:
 * already persisted; denied/failed: gone), so a lingering stash copy
 * could only resurrect a bubble the server can no longer account for —
 * the stuck-forever pending message. No-op when the entry isn't
 * stashed (the common case: the user never navigated away mid-POST).
 */
function removeFromPendingStash(
  stash: Record<string, StashedPending>,
  conversationId: string,
  tempId: string,
): Record<string, StashedPending> {
  const entry = stash[conversationId];
  if (!entry || !entry.messages.some((p) => p.tempId === tempId)) return stash;
  const messages = entry.messages.filter((p) => p.tempId !== tempId);
  const next = { ...stash };
  if (messages.length > 0) next[conversationId] = { ...entry, messages };
  else delete next[conversationId];
  return next;
}

/**
 * Apply store side effects for a `session.*` SSE event.
 *
 * Exported for direct unit testing — production code reaches this
 * via the `tapSessionEvents` generator wrapping the parsed SSE
 * stream inside `pumpStreamEvents`. The reducer (`BlockStream`)
 * deliberately ignores these events; session-scoped state lives on
 * the store, not on the reducer's internal state machine.
 *
 * No-op for events outside the `session.*` family.
 */
export function handleSessionEvent(event: StreamEvent): void {
  switch (event.type) {
    case "response_completed":
      // Prefer contextTokens (last sub-call total) for the context ring — on
      // tool-call turns, totalTokens is the billing sum across all sub-calls
      // which inflates the ring. contextTokens is set only by multi-sub-call
      // executors (e.g. openai-agents); for all others it is null and we fall
      // back to totalTokens, which equals contextTokens for single-call turns.
      if (event.response.usage != null) {
        const ringTokens = event.response.usage.contextTokens ?? event.response.usage.totalTokens;
        if (ringTokens != null) {
          useChatStore.setState({ tokensUsed: ringTokens });
        }
      }
      return;
    case "session_todos":
      // Replace the todo list entirely — each event carries the full
      // current list, not a diff.
      useChatStore.setState({ todos: event.todos });
      return;
    case "session_terminal_pending":
      // Toggle the Terminal-pill spinner. The runner sets pending=true
      // before auto-creating the terminal and clears it once the
      // terminal lands or auto-create fails.
      useChatStore.setState({ terminalPending: event.pending });
      return;
    case "session_sandbox_status":
      // Advance the managed-sandbox provisioning indicator. `ready`
      // clears it — from then on the session looks like any
      // host-bound session; `failed` retains the reason so the page
      // explains why the sandbox never came up.
      useChatStore.setState({
        sandboxStatus: event.stage === "ready" ? null : { stage: event.stage, error: event.error },
      });
      return;
    case "session_usage": {
      // Apply only fields that arrived; a window-only broadcast must
      // not clobber tokensUsed (and vice versa), and a cost-only
      // broadcast (relay path) carries neither token field. The
      // per-bucket breakdown fields follow the same merge rule.
      const patch: {
        tokensUsed?: number;
        contextWindow?: number;
        sessionCostUsd?: number;
        sessionUsageByModel?: Record<string, ModelUsage>;
      } = {};
      if (event.contextTokens !== undefined) {
        patch.tokensUsed = event.contextTokens;
      }
      if (event.contextWindow !== undefined) {
        patch.contextWindow = event.contextWindow;
      }
      if (event.totalCostUsd !== undefined) {
        patch.sessionCostUsd = event.totalCostUsd;
      }
      if (event.usageByModel !== undefined) {
        patch.sessionUsageByModel = event.usageByModel;
      }
      if (Object.keys(patch).length > 0) {
        useChatStore.setState(patch);
      }
      return;
    }
    case "session_model":
      // A `/model` switch made inside the Claude Code terminal. Reflect
      // it in the picker for the open session. The server already
      // persisted `model_override`, so a reload restores it; the
      // cross-session sticky pref is intentionally left untouched (a
      // terminal switch is a per-session choice, not a new default).
      useChatStore.setState({
        selectedModel: event.model,
        sessionModelOverride: event.model,
      });
      return;
    case "session_reasoning_effort":
      // A thinking-level switch made inside a native terminal. Reflect it
      // in the picker for the open session; the server persisted
      // reasoning_effort, so reload restores the same value.
      useChatStore.setState({ selectedEffort: event.reasoningEffort });
      return;
    case "session_presence":
      // Full-state replacement — every presence event carries the
      // complete viewer list, so there is no join/leave ordering to
      // get wrong. Guarded by conversation id so a late frame from a
      // switched-away stream can't paint another session's viewers.
      useChatStore.setState((s) =>
        s.conversationId === event.conversationId ? { viewers: event.viewers } : {},
      );
      return;
    case "session_agent_changed":
      // The session's bound agent was switched in place (switch-agent
      // route). Apply the binding the event itself carries immediately,
      // then re-derive the label-dependent state (most importantly
      // isNativeTerminalSession, which gates the optimistic-bubble
      // lifecycle) from a fresh snapshot — the event is the only signal
      // an in-place switch produces; the URL doesn't change, so the
      // switchTo/bindStream path never re-runs.
      useChatStore.setState((s) =>
        s.conversationId === event.conversationId
          ? { boundAgentId: event.agentId, boundAgentName: event.agentName }
          : {},
      );
      void refreshSessionBinding(event.conversationId);
      // Refresh the header's agent card and the sidebar row for every
      // connected client (the switching client's dialog already does
      // this for itself; observers only learn about it here).
      queryClient?.invalidateQueries({ queryKey: ["session-agent", event.conversationId] });
      queryClient?.invalidateQueries({ queryKey: ["conversations"] });
      // The new agent may sit on the other side of an os_env boundary,
      // flipping Files-tab availability. Mark the environment stale WITHOUT
      // refetching: the server's post-switch runner reset hasn't run yet at
      // this point, so an immediate refetch would re-serve the OLD agent's
      // cached env. The reset publishes session.changed_files.invalidated
      // when done (the prompt refetch); this stale-mark is recovery for a
      // lost reset — the next focus/remount refetch corrects the tab.
      queryClient?.invalidateQueries({
        queryKey: ["workspace-environment", event.conversationId],
        refetchType: "none",
      });
      // The switch closes the old agent's terminals on the runner
      // (reset-state). The runner announces each close with a
      // `session.resource.deleted`, but the terminals cache is
      // SSE-primary with union-on-fetch semantics, so a missed event
      // would leave a dead terminal pinned forever. Reset the cache and
      // refetch from the authoritative endpoint; the new agent's
      // terminal still lands via its own `created` event (the queryFn
      // union keeps any entry that races the fetch).
      queryClient?.setQueryData<TerminalInfo[]>(terminalsQueryKey(event.conversationId), []);
      queryClient?.invalidateQueries({ queryKey: terminalsQueryKey(event.conversationId) });
      return;
    case "compaction_completed":
      // Update the context-ring immediately with the post-compaction token
      // estimate so the ring reflects the reduced context without waiting
      // for the next LLM response.completed event.
      if (event.totalTokens != null) {
        useChatStore.setState({ tokensUsed: event.totalTokens });
      }
      return;
    case "compaction_failed":
      // Compaction failed — history is unchanged. Remove the compaction_loading
      // block so the "Compacting…" shimmer disappears without leaving a marker.
      useChatStore.setState((s) => {
        const idx = [...s.blocks].reverse().findIndex((b) => b.type === "compaction_loading");
        if (idx === -1) return {};
        const realIdx = s.blocks.length - 1 - idx;
        return { blocks: [...s.blocks.slice(0, realIdx), ...s.blocks.slice(realIdx + 1)] };
      });
      return;
    case "policy_denied":
      // Policy denied the user input — drop the optimistic bubble (the
      // server won't emit session.input.consumed for denied inputs, so
      // it would otherwise linger in the transcript). The "Working…"
      // indicator is driven by session.status, not this.
      useChatStore.setState({
        pendingUserMessages: [],
      });
      return;
    case "session_status": {
      // Captured BEFORE the patch below adopts event.responseId, so a
      // running/waiting status carrying an unseen id marks a new turn.
      const prevResponseId = useChatStore.getState().activeResponse?.responseId;
      useChatStore.setState((s) => {
        if (
          event.status === "idle" &&
          event.responseId === undefined &&
          s.activeResponse?.state === "streaming"
        ) {
          // A denied queued input publishes running→idle while the prior
          // response is still streaming. That idle must not clear the
          // prior turn's working signal; response_end owns that lifecycle.
          return {};
        }
        const patch: Partial<ChatState> = { sessionStatus: event.status };
        if (
          event.responseId !== undefined &&
          (event.status === "running" || event.status === "waiting")
        ) {
          patch.status = "streaming";
          patch.activeResponse = {
            responseId: event.responseId,
            state: "streaming",
            error: null,
          };
        }
        if (event.status === "idle" || event.status === "failed") {
          if (event.responseId !== undefined && s.activeResponse?.responseId === event.responseId) {
            patch.status = "idle";
            if (s.activeResponse.state !== "cancelled") {
              patch.activeResponse = {
                responseId: event.responseId,
                state: event.status === "failed" ? "failed" : "completed",
                error: null,
              };
            }
          } else if (s.activeResponse === null) {
            patch.status = "idle";
          }
          // Clear ALL pending user messages on terminal status. Any
          // message still pending when the session reaches idle was
          // either consumed (input.consumed event raced ahead) or
          // denied by policy (no input.consumed fires). In both
          // cases, keeping it in pendingUserMessages would leave a
          // dangling optimistic bubble in the transcript. (The
          // "Working…" indicator no longer reads this — it tracks
          // session.status directly — but the bubble cleanup still
          // matters.)
          //
          // EXCEPT native-terminal sessions (claude/codex-native): their
          // web message isn't persisted at POST time — it round-trips
          // through the vendor TUI and is reconciled by the transcript
          // forwarder's session.input.consumed event, which can arrive
          // AFTER a transient idle/failed (Claude cold-start on resume,
          // runner-relaunch status churn). Clearing here would drop the
          // optimistic bubble before its consumed event lands, leaving a
          // multi-second gap until the committed item re-renders. Native
          // pending bubbles are reconciled by that consumed event (+ the
          // server-side pending_inputs TTL), and native denials roll back
          // via the POST `denied` response — so the idle-clear is never
          // needed for them and only races the round-trip.
          if (!s.isNativeTerminalSession && s.pendingUserMessages.length > 0) {
            patch.pendingUserMessages = [];
          }
        }
        return patch;
      });
      // Refetch the snapshot at turn START too: the runner persists
      // turn-scoped labels (e.g. the cost advisor's `cost_control.plan`
      // verdict) before the harness runs, so the verdict can render
      // mid-turn instead of waiting for the idle/failed refetch below.
      // Once per turn: later running/waiting ticks repeat the responseId
      // the patch above adopted. `exact` spares the heavier
      // ["session", id, "items", ...] queries a mid-turn refetch.
      if (
        event.responseId !== undefined &&
        event.responseId !== prevResponseId &&
        (event.status === "running" || event.status === "waiting")
      ) {
        queryClient?.invalidateQueries({
          queryKey: ["session", event.conversationId],
          exact: true,
        });
      }
      // Patch the active session's row in the sidebar list cache so its
      // status badge flips in lockstep with this live SSE event, instead
      // of lagging up to one 4 s `useConversations` poll behind the
      // chat's "Working…" indicator — the exact desync users hit on a
      // claude-native session (chat clears/sets working instantly while
      // the sidebar dot stays stale).
      patchConversationStatusInCache(event.conversationId, event.status);
      // On turn completion, refresh the Agents-rail preview for this
      // conversation. A child (added agent) finishing a turn leaves a stale
      // last_message_preview in its parent's child-sessions list (the runner
      // can't read a claude-native reply from its in-process history), and the
      // root's own snapshot — which feeds the rail's "main" preview — goes
      // stale the same way. Invalidate the matching query so the fresh,
      // server-computed preview lands without a manual navigate-away.
      if (event.status === "idle" || event.status === "failed") {
        const snapshot = queryClient?.getQueryData<Session>(["session", event.conversationId]);
        if (snapshot?.parentSessionId) {
          // A child finished: refetch its parent's child list for the row's
          // fresh, server-computed preview.
          queryClient?.invalidateQueries({
            queryKey: childSessionsQueryKey(snapshot.parentSessionId),
          });
        } else {
          // Root (or cold-cache) session: its own snapshot carries the
          // rail's "main" preview text.
          queryClient?.invalidateQueries({
            queryKey: ["session", event.conversationId],
          });
        }
      }
      return;
    }
    case "session_input_consumed":
      if (event.isMeta === true) return;
      // Promote the matching optimistic bubble into committed history.
      // Three ways to find it, in order of precision:
      //   1. By id — the server tells us which pending-input entry this
      //      message drained (clearedPendingId = the FIFO-oldest entry's
      //      id), so we drop that exact bubble. Covers snapshot-hydrated
      //      bubbles and optimistic ones whose sender adopted the id.
      //   2. FIFO head — for an optimistic bubble whose POST hasn't
      //      returned the id to adopt yet (consumed raced ahead), or a
      //      cross-client send. Per-session SSE ordering makes the head
      //      the right entry. Unconditional (no text match): the native
      //      transcript reformats text (reply-quote `>` blockquotes,
      //      `[Attached:]` markers), so a text guard would wrongly skip
      //      the drop and strand the bubble as a duplicate.
      //   3. No pending entry — render the event payload as a fresh
      //      committed bubble (TUI-typed message, or another client).
      useChatStore.setState((s) => {
        if (hasCommittedItem(s.blocks, event.itemId)) return {};

        // 1. Drop by id when the server names the drained entry.
        const cleared = event.clearedPendingId;
        if (cleared) {
          const idx = s.pendingUserMessages.findIndex((p) => p.tempId === cleared);
          if (idx >= 0) {
            const matched = s.pendingUserMessages[idx]!;
            const content = committedContentFor(event, matched.content);
            if (content === null) return {};
            return {
              pendingUserMessages: [
                ...s.pendingUserMessages.slice(0, idx),
                ...s.pendingUserMessages.slice(idx + 1),
              ],
              // stableKey = the optimistic bubble's temp id → the
              // promoted bubble keeps the same React key (no remount).
              blocks: [
                ...s.blocks,
                committedUserBlock(
                  event.itemId,
                  content,
                  matched.tempId,
                  event.createdBy ?? matched.author,
                ),
              ],
            };
          }
        }

        // 2. FIFO head fallback (id not adopted yet / cross-client).
        const head = s.pendingUserMessages[0];
        if (head) {
          const content = committedContentFor(event, head.content);
          if (content === null) return {};
          return {
            pendingUserMessages: s.pendingUserMessages.slice(1),
            // stableKey = the popped optimistic bubble's temp id so the
            // promoted bubble keeps the same React key (no remount/flink).
            blocks: [
              ...s.blocks,
              committedUserBlock(
                event.itemId,
                content,
                head.tempId,
                event.createdBy ?? head.author,
              ),
            ],
          };
        }

        // 3. Nothing pending — render the event payload fresh.
        const content = userContentFromEvent(event);
        if (content === null) return {};
        return {
          blocks: [
            ...s.blocks,
            committedUserBlock(event.itemId, content, undefined, event.createdBy),
          ],
        };
      });
      return;
    case "slash_command":
      // Claude-native: a `/skill-name` or surfaced CLI command typed
      // in the web composer round-trips through tmux → Claude TUI →
      // transcript → `external_conversation_item` (type=slash_command)
      // → `response.output_item.done`. The Omnigent server bypasses
      // persistence for these (no `session.input.consumed` fires),
      // so the optimistic bubble in `pendingUserMessages` would
      // otherwise linger next to the rendered SlashCommandBlock
      // until refresh. Pop the FIFO head here to ack the local
      // send; non-empty guard so observing clients (with no pending
      // bubble) just render the block.
      useChatStore.setState((s) => {
        if (s.pendingUserMessages.length === 0) return {};
        const [, ...rest] = s.pendingUserMessages;
        return { pendingUserMessages: rest };
      });
      return;
    case "session_interrupted":
      // Explicit user-cancel signal. Distinguishes "interrupted by
      // user action" from the generic `response.incomplete` that
      // the responses-API path emits. Sets the active response's
      // state to `cancelled`; the response_end branch of the pump
      // becomes a no-op when it sees the existing terminal state.
      if (event.responseId !== undefined) {
        const interruptedResponseId = event.responseId;
        useChatStore.setState((s) => {
          if (s.interruptedResponseIds.includes(interruptedResponseId)) return {};
          return {
            interruptedResponseIds: [...s.interruptedResponseIds, interruptedResponseId],
          };
        });
      }
      finalizeCurrentActive("cancelled", event.responseId);
      return;
    case "session_created":
      // Sub-agent spawn signal. Invalidate the parent's child-sessions
      // query so the execution-log panel re-fetches and renders the
      // new child without waiting for the next poll or manual refresh.
      if (event.conversationId) {
        queryClient?.invalidateQueries({
          queryKey: childSessionsQueryKey(event.conversationId),
        });
      }
      return;
    case "session_resource_created":
      if (event.resource.type === "terminal") {
        applyTerminalCreated(event.resource as unknown as Record<string, unknown>);
      }
      return;
    case "session_resource_deleted":
      if (event.resourceType === "terminal") {
        applyTerminalDeleted(event.sessionId, event.resourceId);
      }
      return;
    case "session_child_session_updated":
      // Child status delta pushed to the parent stream — patch the
      // child-sessions cache in place (no refetch). Also covers the
      // snapshot-on-connect frames, which reuse this event shape.
      applyChildSessionUpdated(event.conversationId, event.childSessionId, event.child);
      // A claude-native child's turn-complete delta carries busy=false but no
      // last_message_preview (its reply lives in the tmux pane, not the
      // runner's in-process history). Refetch the parent's child list so the
      // server-computed preview lands instead of staying stale.
      if (event.child.busy === false && event.child.last_message_preview === undefined) {
        queryClient?.invalidateQueries({
          queryKey: childSessionsQueryKey(event.conversationId),
        });
      }
      return;
    case "session_changed_files_invalidated":
      // Coarse "something changed" signal. Coalesce bursts into one
      // changed-files/root refresh, and mark expanded directory caches
      // stale without immediately refetching every visible folder.
      scheduleWorkspaceFilesystemInvalidation(event.sessionId);
      return;
    case "session_terminal_activity":
      // Runner-determined PTY-output pulse — drives the "active" badge
      // for any terminal without a client attach.
      useTerminalActivityStore.getState().pulse(event.terminalId);
      return;
    case "session_skills":
      // The runner's skills just resolved (server's background fetch
      // populated its cache). Skills are fetched off the snapshot hot
      // path, so the bind-time snapshot served an empty list; this is
      // the first moment the slash-command menu can be filled. Refetch
      // the now-warm snapshot and apply its `skills`. Fire and forget —
      // refetchRunnerBackedSessionState self-guards against a stale apply.
      void refetchRunnerBackedSessionState(event.conversationId);
      return;
    case "session_codex_model_options":
      // Codex app-server `model/list` just resolved. Refetch the
      // cache-warmed snapshot and apply `codexModelOptions`; the picker
      // derives both model rows and effort levels from that catalog.
      void refetchRunnerBackedSessionState(event.conversationId);
      return;
    case "tool_result":
      // Tool results are not a reliable correlation signal for
      // approval cards. In Codex and native harnesses, multiple
      // elicitations can be pending at once while the tool result
      // event carries only a call id, not the elicitation id. The
      // server publishes ``response.elicitation_resolved`` with the
      // exact id when an approval is answered elsewhere; only that
      // event is allowed to flip a pending card to "Resolved
      // elsewhere".
      return;
    case "elicitation_resolved":
      // Match by id, not first-pending — the `pending` guard keeps
      // a user-delivered verdict from being overwritten by a later
      // duplicate-resolve.
      useChatStore.setState((s) => {
        const matchIdx = s.blocks.findIndex(
          (b) =>
            b.type === "elicitation" &&
            b.elicitationId === event.elicitationId &&
            b.status === "pending",
        );
        if (matchIdx === -1) return {};
        const target = s.blocks[matchIdx] as ElicitationBlock;
        const updated: ElicitationBlock = {
          ...target,
          status: "responded",
          response: { action: "auto_resolved" },
        };
        return {
          blocks: [...s.blocks.slice(0, matchIdx), updated, ...s.blocks.slice(matchIdx + 1)],
        };
      });
      return;
  }
}

/**
 * Patch the active session's row in the sidebar conversations cache so
 * its status badge tracks the live ``session.status`` SSE event instead
 * of lagging up to one ``useConversations`` poll (4 s) behind the chat's
 * "Working…" indicator.
 *
 * Mirrors the server's list-status collapse (``GET /v1/sessions`` in
 * ``sessions.py``): ``running``/``waiting`` → ``"running"``, ``failed``
 * → ``"failed"``, ``idle`` → ``"idle"``. The next list poll re-confirms
 * the same value — the server's ``_session_status_cache`` was written by
 * the same event — so this never fights the poller. Only the active
 * session has a bound stream, so only its row updates live; other rows
 * still reconcile on the poll, which is exactly the badge the user
 * compares against the open chat.
 *
 * No-ops (returns the cached reference unchanged) when the row is absent
 * or already shows the target status, so repeated ``running`` ticks
 * don't churn the sidebar.
 */
function patchConversationStatusInCache(
  conversationId: string,
  sessionStatus: SessionStatus,
): void {
  if (queryClient === null) return;
  const listStatus: NonNullable<Conversation["status"]> =
    sessionStatus === "running" || sessionStatus === "waiting"
      ? "running"
      : sessionStatus === "failed"
        ? "failed"
        : "idle";
  queryClient.setQueriesData<InfiniteData<ConversationsPage>>(
    { queryKey: ["conversations"] },
    (data) => {
      if (!data) return data;
      let mutated = false;
      const pages = data.pages.map((page) => {
        const idx = page.data.findIndex((c) => c.id === conversationId);
        if (idx === -1 || page.data[idx].status === listStatus) return page;
        mutated = true;
        const nextData = [...page.data];
        nextData[idx] = { ...nextData[idx], status: listStatus };
        return { ...page, data: nextData };
      });
      return mutated ? { ...data, pages } : data;
    },
  );
}

/**
 * Patch the terminals query cache to include a newly-created terminal.
 *
 * Initializes the cache when cold (``undefined``) rather than skipping:
 * snapshot-on-connect emits a ``session.resource.created`` for every
 * currently-running terminal, so the baseline the old skip was guarding
 * against now arrives over the stream. Seeding here is what lets the
 * terminal count update live even when no ``useTerminals`` is mounted
 * (e.g. the panel is closed) — otherwise the count only moved on the
 * response-end refetch (turn boundary). Idempotent by id.
 */
function applyTerminalCreated(resource: Record<string, unknown>): void {
  const sessionId = resource.session_id;
  if (typeof sessionId !== "string" || !sessionId) return;
  const info = terminalInfoFromResource(resource);
  if (info === null) return;
  if (queryClient === null) return;
  const key = terminalsQueryKey(sessionId);
  const current = queryClient.getQueryData<TerminalInfo[]>(key) ?? [];
  if (current.some((t) => t.id === info.id)) return;
  queryClient.setQueryData<TerminalInfo[]>(key, [...current, info]);
}

/**
 * Patch the terminals query cache to drop a deleted terminal.
 *
 * Idempotent: a delete for an unknown id is a no-op.
 */
function applyTerminalDeleted(sessionId: string, resourceId: string): void {
  if (queryClient === null) return;
  const key = terminalsQueryKey(sessionId);
  const current = queryClient.getQueryData<TerminalInfo[]>(key);
  if (current === undefined) return;
  const next = current.filter((t) => t.id !== resourceId);
  if (next.length === current.length) return;
  queryClient.setQueryData<TerminalInfo[]>(key, next);
}

/**
 * Upsert-with-merge a child session into the parent's query cache.
 *
 * The event payload is a PARTIAL ``ChildSessionInfo``: snapshot-on-connect
 * carries the full summary, but live runner deltas carry only what
 * changed (a status delta omits ``last_message_preview``; a preview delta
 * carries only it). So we overlay *present* fields onto the existing row
 * (a status flip keeps the preview, a preview update keeps busy/status),
 * and insert from present fields when the child isn't cached yet. Cold
 * cache (parent not viewed since reload) is a no-op — the eventual
 * ``useChildSessions`` mount pulls a fresh list.
 */
function applyChildSessionUpdated(
  parentId: string,
  childId: string,
  child: Record<string, unknown>,
): void {
  if (queryClient === null) return;
  const key = childSessionsQueryKey(parentId);
  // Initialize a cold cache rather than skipping: snapshot-on-connect
  // sends full child rows over the stream, so seeding here lets child
  // status/preview update live even when no useChildSessions is mounted.
  const current = queryClient.getQueryData<ChildSessionInfo[]>(key) ?? [];

  // Build a patch from only the fields PRESENT in the payload (undefined
  // = "not in this delta, leave as-is"); explicit null is a real value.
  const patch: Partial<ChildSessionInfo> = {};
  const strOrNull = (v: unknown): string | null => (typeof v === "string" ? v : null);
  const strRecordOrEmpty = (v: unknown): Record<string, string> =>
    v && typeof v === "object"
      ? Object.fromEntries(
          Object.entries(v).filter(
            (entry): entry is [string, string] =>
              typeof entry[0] === "string" && typeof entry[1] === "string",
          ),
        )
      : {};
  const errorOrNull = (v: unknown): ChildSessionInfo["last_task_error"] => {
    if (!v || typeof v !== "object") return null;
    const record = v as Record<string, unknown>;
    if (typeof record.code !== "string" || typeof record.message !== "string") return null;
    if (!record.code || !record.message) return null;
    return { code: record.code, message: record.message };
  };
  if (child.title !== undefined) patch.title = strOrNull(child.title);
  if (child.tool !== undefined) patch.tool = strOrNull(child.tool);
  if (child.session_name !== undefined) patch.session_name = strOrNull(child.session_name);
  if (child.labels !== undefined) patch.labels = strRecordOrEmpty(child.labels);
  if (child.current_task_status !== undefined)
    patch.current_task_status = strOrNull(child.current_task_status);
  if (child.last_task_error !== undefined)
    patch.last_task_error = errorOrNull(child.last_task_error);
  if (child.busy !== undefined) patch.busy = child.busy === true;
  if (child.last_message_preview !== undefined)
    patch.last_message_preview = strOrNull(child.last_message_preview);
  if (child.pending_elicitations_count !== undefined)
    patch.pending_elicitations_count =
      typeof child.pending_elicitations_count === "number" ? child.pending_elicitations_count : 0;

  const idx = current.findIndex((c) => c.id === childId);
  if (idx === -1) {
    // Insert: absent fields default (null / not-busy) until a fuller
    // update (snapshot/refetch) fills them in.
    const inserted: ChildSessionInfo = {
      id: childId,
      title: patch.title ?? null,
      tool: patch.tool ?? null,
      session_name: patch.session_name ?? null,
      labels: patch.labels ?? {},
      current_task_status: patch.current_task_status ?? null,
      last_task_error: patch.last_task_error ?? null,
      busy: patch.busy ?? false,
      last_message_preview: patch.last_message_preview ?? null,
      pending_elicitations_count: patch.pending_elicitations_count ?? 0,
    };
    queryClient.setQueryData<ChildSessionInfo[]>(key, [inserted, ...current]);
    return;
  }
  const next = [...current];
  next[idx] = { ...current[idx], ...patch };
  queryClient.setQueryData<ChildSessionInfo[]>(key, next);
}

async function* tapSessionEvents(events: AsyncIterable<StreamEvent>): AsyncIterable<StreamEvent> {
  for await (const event of events) {
    handleSessionEvent(event);
    yield event;
  }
}

/**
 * Force the store's `activeResponse` into a terminal state without
 * needing a closure-scoped setter. Mirrors `finalizeActive` but
 * works from the module-scope `handleSessionEvent` boundary.
 */
function finalizeCurrentActive(state: ActiveResponse["state"], responseIdOverride?: string): void {
  useChatStore.setState((s) => {
    if (s.activeResponse === null && responseIdOverride === undefined) return {};
    const responseId = s.activeResponse?.responseId ?? responseIdOverride ?? "";
    return {
      activeResponse: { responseId, state, error: null },
    };
  });
}

/**
 * Move `activeResponse` to a terminal state. The matching assistant
 * bubble keeps showing the cancelled / failed marker until the next
 * send clears it (`send` nulls `activeResponse` at the start of each
 * new send).
 *
 * `responseIdOverride` is used by error paths when activeResponse's
 * `responseId` was never populated (e.g. send threw before any
 * response.created fired). Pass `null` to leave the value untouched.
 */
function finalizeActive(
  set: Setter,
  state: ActiveResponse["state"],
  error: string | null,
  responseIdOverride?: string | null,
): void {
  set((s) => {
    if (s.activeResponse === null && !responseIdOverride) return {};
    const responseId = s.activeResponse?.responseId ?? responseIdOverride ?? "";
    return {
      activeResponse: { responseId, state, error },
    };
  });
}

// Mirrors the server's ErrorCode.RUNNER_UNAVAILABLE (omnigent/errors.py) —
// the 503 returned by POST /events when a host-bound runner never connects
// within the connect-grace + relaunch window.
const RUNNER_UNAVAILABLE_CODE = "runner_unavailable";

/**
 * Turn a thrown send failure into user-facing banner text + a code.
 *
 * The runner-unavailable 503 gets self-explanatory copy (and no raw code in
 * the banner title) so a slow/never-online runner reads as a clear, retryable
 * message rather than the server's terse "No runner bound for session". Other
 * failures fall back to the error's own message, carrying the machine code
 * when present for debuggability.
 */
function describeSendFailure(err: unknown): { message: string; code: string } {
  if (err instanceof ApiError && err.code === RUNNER_UNAVAILABLE_CODE) {
    return {
      message: "The runner didn't come online in time. Please try again.",
      code: "",
    };
  }
  if (err instanceof ApiError) {
    return { message: err.message, code: err.code ?? "" };
  }
  return { message: err instanceof Error ? err.message : String(err), code: "" };
}

/**
 * A client-only {@link ErrorBlock} for a send that failed before any turn
 * started (so no server `response.error` / `last_task_error` will ever
 * render it). Ephemeral — it lives only in `blocks` and is dropped on the
 * next `switchTo` / rebind, matching the transient nature of the failure.
 */
function makeClientErrorBlock(message: string, code: string): ErrorBlock {
  return {
    type: "error",
    ctx: { agent: null, depth: 0, turn: 0, timestamp: 0, responseId: "", itemId: null },
    message,
    source: "",
    code,
  };
}
