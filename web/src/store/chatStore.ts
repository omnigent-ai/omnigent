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
  ToolGroup,
  UserMessageBlock,
} from "@/lib/blocks";
import { userInputElicitationKey } from "@/lib/askUserQuestion";
import { LIVE_ITEM_PREFIX, structuredErrorFields } from "@/lib/blocks";
import { BlockStream } from "@/lib/blockStream";
import { itemsToBlocks } from "@/lib/itemsToBlocks";
import { emitBrowserActionRequest } from "@/lib/browserActionBus";
import {
  ApiError,
  approve as approveElicitation,
  bindOnlyOnlineRunner,
  createSession,
  getSessionSlim,
  fetchSessionItemsPage,
  INITIAL_WINDOW_ITEMS,
  interrupt as interruptSession,
  openSessionStream,
  postEvent,
  type SessionItemsPage,
  updateSession,
} from "@/lib/sessionsApi";
import type {
  McpServerStartup,
  SessionInputConsumedEvent,
  SessionViewer,
  StreamEvent,
} from "@/lib/events";
import { createPresenceIdleTracker } from "@/lib/presenceIdle";
import { conversationRegistry, type ConversationEntry } from "./conversationRegistry";
import { createInitialConversationState, isConversationStateKey } from "./conversationState";
import { getStreamSlotManager, type StreamSlot } from "./streamSlots";
import {
  SSE_STALL_TIMEOUT_MS,
  parseEvent,
  parseSseStream,
  withStallGuard,
  type SseStreamResult,
} from "@/lib/sse";
import { clearSseLog, pushSseEvent } from "@/lib/sseEventLog";
import { childSessionsQueryKey, type ChildSessionInfo } from "@/hooks/useChildSessions";
import { sessionItemsQueryKey } from "@/hooks/useSessionItems";
import type { Conversation, ConversationsPage } from "@/hooks/useConversations";
import { overlayTitleIntoCaches, type ConversationsInfiniteData } from "@/lib/sessionListCache";
import { useTerminalActivityStore } from "./terminalActivity";
import { terminalInfoFromResource, terminalsQueryKey, type TerminalInfo } from "@/lib/terminals";
import type {
  BackgroundTaskInfo,
  ContentBlock,
  ModelUsage,
  NativeModelOption,
  PendingInput,
  SandboxStatus,
  Session,
  SessionStatus,
  SkillSummary,
} from "@/lib/types";
import { uploadFile } from "@/lib/filesApi";
import type { ActiveResponse } from "./types";
import { supportsEffortControl } from "@/lib/sessionCapabilities";
import { claudePermissionModeFromSession } from "@/lib/claudePermissionMode";
import { codexPlanModeFromSession } from "@/lib/codexPlanMode";
import { getCurrentAuthorId } from "@/lib/identity";
import { getOmnigentHostConfig, type OmnigentInteractionStatus } from "@/lib/host";
// Routing-free emit primitive (not "@/lib/analytics", which pulls in useLocation
// and would form a routing↔store import cycle).
import { emitInteractionPhase } from "@/lib/analyticsEmit";
import { getSessionHost } from "@/lib/sessionHost";
import { isSystemUserContent } from "@/lib/systemMessage";
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
  /** Client epoch seconds stamped ONCE at send time — the optimistic
   *  bubble's display timestamp. Stamping here (not at render or
   *  promotion) keeps the shown time pinned to when the user hit send.
   *  Absent on snapshot-replayed entries (server carries no stamp). */
  createdAtS?: number;
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
 * A message the user submitted while the agent was busy. It is held
 * client-side — NOT yet POSTed — and shown in the docked queue strip above
 * the composer until the agent goes idle, when the head is flushed FIFO (one
 * per turn). This is the opposite of {@link PendingUserMessage}, which is
 * already POSTed and renders as an optimistic bubble in the transcript.
 *
 * In-memory only: a hard reload clears the queue, so `files` can be held
 * directly (no serialization concern).
 */
export interface QueuedMessage {
  /** Client-only id, e.g. `q_1`. */
  queueId: string;
  /** Fully-assembled message text (mentions/quotes already applied). */
  text: string;
  /** Attachments to send with the message. */
  files?: File[];
  /** Owning conversation, so a switch/idle only flushes its own queue. */
  conversationId: string;
  /**
   * Agent bound when the message was queued, so it flushes to the agent it was
   * composed for even if the binding changed meanwhile (e.g. a `/model` switch).
   * Falls back to the current `boundAgentId` when absent.
   */
  agentId?: string;
}

/**
 * A workspace path queued for the composer's "@"-mention chips. ``isDir``
 * marks a folder (delivered with a trailing ``/``); ``lineRange`` marks a
 * specific span of a file (delivered as ``path:start-end``), e.g. from the
 * file viewer's "Attach to agent" button.
 */
export interface ComposerAttachment {
  path: string;
  isDir: boolean;
  lineRange?: { start: number; end: number };
}

/**
 * Identity key for a composer attachment, used to dedup the queue and the
 * drained chips. Keyed on path + dir-ness + line range (not path alone) so a
 * whole-file attach and a partial-line attach of the same file — or two
 * distinct line ranges from the file viewer — remain separate, while an exact
 * re-attach is collapsed. The single source of truth for "same attachment?"
 * across the store queue, the drain effect, and ``attachMention``.
 */
export function composerAttachmentKey(a: ComposerAttachment): string {
  return `${a.path}|${a.isDir}|${a.lineRange ? `${a.lineRange.start}-${a.lineRange.end}` : ""}`;
}

/**
 * State owned by a SINGLE conversation.
 *
 * Every field here describes one conversation: its transcript, turn
 * lifecycle, binding, usage, and stream. Each lives on its own registry entry
 * (see `conversationRegistry`), projected onto the root store for whichever
 * conversation is on screen. A new field belongs here only if it still makes
 * sense for a conversation the user is NOT looking at.
 */
export interface ConversationState {
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
  backgroundTaskCount: number;
  /**
   * Per-shell detail behind `backgroundTaskCount`, kept in lockstep with it,
   * so the UI can list each running background shell. Empty when none are
   * tracked (or an older runner reported only the count).
   */
  backgroundTasks: BackgroundTaskInfo[];
  /**
   * Why a still-`running` session is parked, e.g. "permission prompt".
   * Terminal-backed agents can block on a dialog the web UI does not
   * mirror, so the working indicator names it instead of shimmering with
   * no explanation. `null` whenever the session is not parked.
   */
  blockedOn: string | null;
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
   * Whether this is a native-terminal wrapper whose model is chosen inside the
   * vendor TUI (qwen/goose/cursor/pi/opencode) rather than through an Omnigent
   * model picker. The composer status line hides its model/effort label for
   * these — Omnigent's bound `llmModel` is just an unused default (it would
   * otherwise read e.g. "claude-sonnet-4-6" on a Qwen session). claude-/codex-
   * native DO expose an Omnigent picker, so they keep the label. `false` on
   * `/`, before the snapshot resolves, and for non-native sessions.
   */
  nativeVendorOwnsModel: boolean;
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
   * This session's effective reasoning effort — the server's persisted
   * ``reasoning_effort`` once hydrated, else the sticky pick applied on bind.
   * ``null`` when the harness has no effort control or none is set.
   *
   * Conversation-scoped for the same reason as ``sessionModelOverride``: two
   * live conversations can sit at different efforts, and a warm switch back
   * re-projects this rather than re-binding. ``selectedEffort`` remains the
   * single app-global sticky pick used for cross-session restore, so it cannot
   * answer "what is THIS conversation at".
   */
  sessionReasoningEffort: string | null;
  /**
   * Per-session cost-control switch for the active session: ``"on"``
   * activates the spec's configured cost-control mode, ``"off"``
   * disables cost control, ``null`` defers to the spec default.
   * Session-scoped (NOT a sticky pick): hydrated from the session
   * snapshot on bind and written through `setCostControlMode`.
   */
  costControlModeOverride: "on" | "off" | null;
  /**
   * Routing switch for the sub-agents the active session spawns: ``"on"``
   * routes them, and ``"off"`` runs them on the default model. ``null``
   * comes back for a session created before the switch became explicit and
   * reads the same as ``"off"``. Session-scoped: hydrated from the snapshot
   * on bind and written through `setSubagentRouting`.
   */
  subagentRoutingOverride: "on" | "off" | null;
  /**
   * Per-session Codex collaboration-mode flag. Hydrated from
   * ``omnigent.codex_native.collaboration_mode`` on bind and updated by the
   * web toggle or native Codex TUI events. False for non-Codex sessions.
   */
  codexPlanMode: boolean;
  /**
   * Permission mode of a running claude-native session, e.g. ``"auto"``.
   * Hydrated from ``omnigent.claude_native.permission_mode`` on bind
   * (falling back to the launch flag) and updated by the composer's mode
   * picker. Empty string when unknown — a non-Claude session, or a mode set
   * via ``permissions.defaultMode`` that never reaches the launch args. The
   * composer hides the picker rather than showing a guessed mode.
   */
  claudePermissionMode: string;
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
  /**
   * The text + attachments of a send that failed before the server took
   * ownership of it, handed back so the composer can restore them for a
   * retry. Without this the message is simply gone: `submit` clears the
   * composer optimistically, and a first message carried in from the
   * landing screen has already had its draft dropped and its pending
   * prompt destructively consumed — so an upload 415 or a runner 503 left
   * the user with an error and nothing to resend. The composer drains this
   * (matching on conversation id) and clears it.
   *
   * Single-slot: a second failure in the same session replaces the first's
   * retained draft (last failure wins). A send that fails before any session
   * id resolves isn't captured — there is no composer keyed to restore it
   * into — but the landing path binds a session first, so the reported flow
   * is covered.
   */
  failedSendDraft: { conversationId: string; text: string; files: File[] } | null;
  /**
   * When a send last latched THIS conversation's `status` to "streaming", or
   * `null`. Conversation-scoped, not a module global, because `status` is now
   * per-conversation: two conversations can each hold a hung send, and a single
   * shared timestamp would let recovering one strand the other (its `status`
   * stays "streaming" but can no longer age out — see `sendLatchIsStranded`).
   * Lives on the entry so the timestamp and the `status` it guards always
   * travel together (adoption on new-chat, eviction, mirroring).
   */
  sendLatchedAt: number | null;
  /**
   * LLM model identifier from the bound agent's spec for the active
   * session, e.g. ``"anthropic/claude-sonnet-4-6"``. Populated from
   * the session snapshot on bind; ``null`` before bind or when the
   * agent has no explicit model.
   */
  llmModel: string | null;
  /**
   * A model switch the harness has not confirmed yet: the requested model
   * from the moment the PATCH lands until the harness's own report
   * (``session.model``) or a ``model_change_not_applied`` error settles it.
   * Drives the composer's pending indicator; never display state for the
   * chip itself (the chip keeps showing ``llmModel`` — the truth).
   */
  pendingModelChange: string | null;
  /**
   * Effective brain harness for the active session (override-aware),
   * e.g. ``"claude-sdk"`` or ``"pi"``. Populated from the session
   * snapshot on bind; drives the composer pill's harness suffix.
   */
  sessionHarness: string | null;
  /**
   * The active session's sub-agent head name (e.g. `"gpt"`), or null for a
   * top-level session. Set from the snapshot on bind; lets a head sub-agent's
   * composer identity name the head rather than the bundle orchestrator.
   */
  subAgentName: string | null;
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
  todos: {
    content: string;
    status: "pending" | "in_progress" | "completed";
    activeForm: string;
  }[];
  /**
   * Skills the bound agent can invoke (bundled + host-discovered).
   * Populated from the session snapshot on bind; empty array
   * before bind. The composer's slash-command menu reads this to
   * suggest ``/skill-name``.
   */
  skills: SkillSummary[];
  /** Runner-owned model picker rows for the active native session. */
  codexModelOptions: NativeModelOption[];
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
   * Epoch ms when this client last asked a host to launch a runner for the
   * open session outside the send path — today, a host switch. The runner
   * is coming up but nothing on the wire says so yet: the session is not
   * newly created, so the liveness startup grace doesn't apply, and no turn
   * is in flight, so it would otherwise read as idle `runner_asleep` and
   * show nothing at all. Feeds `useSessionLiveness` as a `starting` nudge
   * and self-expires after `STARTING_GRACE_S`. `null` when no such launch
   * is outstanding.
   */
  runnerLaunchedAt: number | null;
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
  /**
   * Per-MCP-server startup map for the bound session (codex-native).
   * Updated by `session.mcp_startup` SSE events while the harness boots
   * its MCP servers; cleared back to `null` once every server settles
   * `ready`. Failed/cancelled servers are retained so the page can say
   * which servers never came up. Always `null` for sessions whose
   * harness reports no MCP startup.
   */
  mcpStartup: Record<string, McpServerStartup> | null;

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
}

/**
 * State that belongs to the APP, not to any one conversation.
 *
 * Sticky picker prefs span sessions; the rest describe the single active
 * view (which conversation is on screen, what its composer is holding).
 * These stay on the root store when per-conversation state moves out.
 */
export interface AppChatState {
  /** The conversation currently on screen. `null` on `/`. */
  conversationId: string | null;
  /**
   * Set when a live `session.superseded` event asks the client to follow
   * the active conversation to another one (e.g. after a Claude `/clear`).
   * `ChatPage` observes this, navigates to `/c/<id>` (replacing history so
   * Back doesn't return to the cleared session), then clears it. Null when
   * no redirect is pending. The store can't call react-router directly, so
   * it hands the target to the page via this field. Live-only — a reload of
   * the old conversation renders the persisted notice instead.
   */
  redirectToConversationId: string | null;
  /**
   * Messages submitted while the agent is busy, held client-side (not yet
   * POSTed) and shown in the composer's queue strip. The head is flushed
   * FIFO — one per turn — when the session goes idle. In-memory only.
   *
   * One flat array across ALL conversations (each entry carries its
   * `conversationId`), so it is app-global rather than per-conversation:
   * `flushBackgroundQueues` drains conversations the user has navigated
   * away from.
   */
  queuedMessages: QueuedMessage[];
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
  /** Bubble that should pulse briefly (highlight on nav jump). */
  flashItemId: string | null;
  /**
   * Workspace files/folders queued to drop into the active composer's
   * "@"-mention chips from outside the composer — e.g. the file viewer's
   * "Attach to agent" button, which lives far from the composer in the tree.
   * The composer drains this on change and clears it.
   */
  pendingComposerAttachments: ComposerAttachment[];
  /**
   * True when this tab could not take an origin-wide stream slot for a
   * conversation it needed to open: every slot is held by other tabs and this
   * tab had no background stream of its own to reclaim. The active conversation
   * still opens (over budget), so the app keeps working; the banner warns the
   * user to close tabs. Cleared once this tab holds a slot again.
   */
  streamBudgetExceeded: boolean;
  /**
   * Whether the user dismissed the too-many-tabs banner for the CURRENT
   * over-budget episode. Reset when `streamBudgetExceeded` goes false→true, so a
   * fresh episode re-shows it rather than nagging within one episode.
   */
  streamBudgetBannerDismissed: boolean;
}

/** Actions exposed on the root store. */
export interface ChatActions {
  send: (text: string, agentId: string, files?: File[], opts?: SendOptions) => Promise<void>;
  /**
   * Queue a message client-side instead of POSTing it now, for a send made
   * while the agent is busy. The head is flushed automatically (FIFO, one per
   * turn) when the session next goes idle — see the `session_status` handler.
   */
  enqueueMessage: (text: string, files?: File[]) => void;
  /** Remove a queued message by id (the strip's per-row delete). */
  dequeueMessage: (queueId: string) => void;
  /**
   * Reorder a queued message within its own conversation (the strip's
   * drag-to-reorder). Moves `queueId` so it sits before `beforeQueueId`, or to
   * the end of its conversation's run when `beforeQueueId` is null. Only
   * reorders among the same conversation's messages — the flat `queuedMessages`
   * array interleaves conversations, so other conversations' entries keep their
   * absolute positions. No-op if the id isn't queued or the move is a no-op.
   */
  reorderQueuedMessage: (queueId: string, beforeQueueId: string | null) => void;
  /**
   * Send a queued message NOW instead of waiting for the idle flush (the
   * strip's per-row steer). Removes it from the queue and POSTs it: on an
   * SDK harness the server live-injects it into the running turn; the
   * optimistic bubble promotes on POST. No-op if the id isn't queued.
   */
  steerMessage: (queueId: string) => void;
  /**
   * Drop all queued messages for a conversation. Called when a conversation is
   * deleted so its queue can't linger in memory (it would never flush — you
   * can't be bound to a deleted session).
   */
  clearQueuedMessages: (conversationId: string) => void;
  /**
   * Flush the queue head if the session is idle and ready. Level-triggered:
   * safe to call on any state change (idempotent — no-ops when busy, when the
   * queue is empty, or when the head isn't for the bound conversation). POSTing
   * the head starts a turn → the session goes busy → this no-ops until the next
   * idle, so the queue drains FIFO one per turn.
   */
  maybeFlushQueuedHead: () => void;
  /**
   * Flush queued messages for conversations OTHER than the active one, whose
   * status in the `["conversations"]` cache is idle. The active conversation is
   * owned by {@link maybeFlushQueuedHead}; this covers a queue whose session the
   * user has navigated away from (its SSE stream is gone, so it can't drain
   * itself). Sends one message per idle conversation per call: uploads any
   * attachments then posts via `postEvent` — the same two-phase sequence
   * send() runs (no active-session state touched, no optimistic bubble — it
   * re-hydrates on return). Level-triggered + idempotent; safe to over-fire.
   */
  flushBackgroundQueues: () => void;
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
   *
   * ``expectConfirmation`` marks the pick pending until the harness's own
   * report (or a not-applied error) settles it — pass it for sessions
   * whose chip renders the reported model (claude-/codex-native).
   */
  setModel: (model: string | null, opts?: { expectConfirmation?: boolean }) => Promise<void>;
  /**
   * Set the active session's cost-control switch — optimistic local
   * flip, then PATCH; the server's canonical value (or a rollback on
   * failure) settles the state. ``null`` clears back to the spec
   * default. No-ops when there is no active conversation.
   */
  setCostControlMode: (mode: "on" | "off" | null) => Promise<void>;
  /**
   * Set the active session's sub-agent routing switch — optimistic local
   * write, then PATCH; the server's canonical value (or a rollback on
   * failure) settles the state. Two-state: ``"off"`` is the way back to
   * unrouted sub-agents. No-ops when there is no active conversation.
   */
  setSubagentRouting: (mode: "on" | "off") => Promise<void>;
  /**
   * Re-read the active session's routing switches (cost control + sub-agent
   * routing) from the server and apply them.
   *
   * Both are hydrated on bind only — no SSE event carries them and the
   * ``["session", id]`` query never goes stale — so a change made elsewhere
   * (another tab, the CLI, a collaborator) would otherwise stay invisible for
   * the life of the tab, and a control seeded from that stale state would show
   * a value the session no longer has. Callers re-read before showing the
   * switches. Best-effort: a failed fetch or a session switch mid-flight
   * leaves the current state alone.
   */
  refreshSessionOverrides: () => Promise<void>;
  /**
   * Toggle Codex Plan mode for the active session. No-ops when there is no
   * active conversation.
   */
  setCodexPlanMode: (enabled: boolean) => Promise<void>;
  /**
   * Switch a running claude-native session's permission mode (e.g. to
   * ``"auto"``). Rejects when the live TUI could not reach the mode, so
   * callers surface the error rather than assuming the switch landed.
   * No-ops when there is no active conversation.
   */
  setClaudePermissionMode: (mode: string) => Promise<void>;
  /**
   * Fetch the next page of older messages and prepend them to `blocks`.
   *
   * No-ops when `hasMoreHistory` is false, `loadingMoreHistory` is true,
   * or there is no active conversation / oldest-item cursor yet.
   */
  loadMoreHistory: () => Promise<void>;
  /** Flash a bubble briefly; rapid calls reschedule so the latest target wins. */
  flashUserMessage: (itemId: string) => void;
  /** Queue an "@"-mention chip into the active composer from outside it. */
  addComposerAttachment: (attachment: ComposerAttachment) => void;
  /** Drain the queued composer attachments (called by the composer). */
  clearPendingComposerAttachments: () => void;
  /** Stamp {@link ChatState.runnerLaunchedAt} now — call right after a
   *  successful `launchRunner` for the open session. */
  markRunnerLaunched: () => void;
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
  /** Dismiss the too-many-tabs banner for the current over-budget episode. */
  dismissStreamBudgetBanner: () => void;
}

/**
 * The root store's shape: the active conversation's state, flattened
 * alongside app-global state and the actions.
 *
 * The `ConversationState` half is a projection of whichever conversation is
 * active. Reading it is how components stay agnostic about that; writing it
 * is how the single active conversation is driven today.
 */
export interface ChatState extends ConversationState, AppChatState, ChatActions {}

let queryClient: QueryClient | null = null;

/**
 * Evict a conversation from the live registry.
 *
 * Called when a conversation is deleted: its entry must stop pumping and let go
 * of its stream.
 */
export function releaseConversation(id: string): void {
  conversationRegistry.release(id);
}

// Catalogs that resolved while their bind snapshot was still hydrating.
const racedNativeModelOptions = new Map<string, NativeModelOption[]>();
let pendingSeq = 0;
let queueSeq = 0;
// When a send last latched local `status` to "streaming". Stamped on the way in
// and never cleared on the way out: after a normal turn `status` settles to
// "idle" on its own, so a leftover value is inert — only a `status` still
// reading "streaming" long afterwards makes it meaningful. The timestamp lives
// on each conversation's entry (`ConversationState.sendLatchedAt`), not here,
// so two hung sends can't share one scalar — see that field's note.

// A chain link must never be able to deadlock its successors. `postEvent`
// issues its fetch with no timeout, so a connection that dies mid-flight never
// settles and the send's `finally` never releases its link. Waiting on the
// prior send is therefore bounded: past this the successor proceeds anyway and
// only ordering degrades, which beats a composer that queues forever with no
// error and no recovery short of a page reload. Set well above any legitimate
// POST — a message can block on runner session-init or a sandbox-host wake,
// which take minutes — so this only ever fires on a send that is truly stuck,
// never reordering a slow one.
const SEND_CHAIN_MAX_WAIT_MS = 180_000;

/**
 * Read one conversation's server-side status from the sidebar cache.
 *
 * The `["conversations"]` cache is kept live by the WS `/v1/sessions/updates`
 * overlay and the poll, so it is the one view of a session's real status that
 * does not depend on this tab's own send lifecycle.
 *
 * @returns the row's status, or `undefined` when no loaded page holds the row.
 */
function cachedConversationStatus(conversationId: string): string | undefined {
  if (queryClient === null) return undefined;
  for (const [, data] of queryClient.getQueriesData<ConversationsInfiniteData>({
    queryKey: ["conversations"],
  })) {
    for (const page of data?.pages ?? []) {
      for (const row of page.data) {
        if (row.id === conversationId) return row.status;
      }
    }
  }
  return undefined;
}

/**
 * Whether local `status: "streaming"` is a stranded latch rather than a live send.
 *
 * `postEvent` issues its fetch with no timeout, so a POST whose connection dies
 * mid-flight never settles: `send`'s `finally` never runs and `status` stays
 * "streaming" forever. That flag gates both the composer and the queue, so every
 * later message queues with no error and no recovery short of a page reload. A
 * turn whose terminal SSE edge was lost strands it the same way.
 *
 * Nothing separates a stuck send from a slow one except elapsed time, so the
 * latch is overridden only once it has outlived any plausible POST AND the
 * session's own row disagrees with it. A live streaming response is never stale.
 */
function sendLatchIsStranded(s: ChatState): boolean {
  if (s.sendLatchedAt === null || Date.now() - s.sendLatchedAt < SEND_CHAIN_MAX_WAIT_MS)
    return false;
  if (s.activeResponse?.state === "streaming") return false;
  return s.conversationId !== null && cachedConversationStatus(s.conversationId) === "idle";
}

// One send chain per conversation, keyed by conversation id. A `send` waits on
// the previous send TO THE SAME CONVERSATION before issuing its POST, so
// rapid-fire messages reach the server in submission order (concurrent `fetch`
// POSTs have no ordering guarantee). Per conversation, not global: ordering is
// only meaningful within a session, and a shared chain lets a stalled send to
// one conversation delay an unrelated one. Chains only ever resolve, never
// reject.
//
// The chain is a mutable box rather than a bare promise so a new chat's chain
// can be RE-KEYED as one unit. Its `tail` is whatever the most recent entrant
// is waiting to release, so migrating the box carries every already-queued
// follower with it; replacing a bare promise under the new key would strand
// them behind the wrong link. See `enterSendChain`.
interface SendChain {
  tail: Promise<void>;
}
const sendChains = new Map<string | symbol, SendChain>();

// Sends with no conversation id yet (brand-new chat) serialize together: the
// session is created inside the chained work, so they can't key by id. A
// non-string key can never collide with a conversation id.
const NEW_SESSION_SEND_CHAIN_KEY = Symbol("new-session");

/**
 * Take a slot in a conversation's send chain.
 *
 * :param conversationId: Conversation to serialize against, or ``null`` for a
 *     send whose session doesn't exist yet (a brand-new chat). Null callers
 *     share one chain, which is what orders the create-then-post race.
 * :returns: ``waitForPrior`` (a bounded wait on the prior send), ``rekey`` to
 *     call once a new chat's real session id is known, and ``releaseSend`` to
 *     call after the work settles (in a ``finally``).
 */
function enterSendChain(conversationId: string | null): {
  waitForPrior: () => Promise<void>;
  rekey: (resolvedConversationId: string) => void;
  releaseSend: () => void;
} {
  const key: string | symbol = conversationId ?? NEW_SESSION_SEND_CHAIN_KEY;
  let chain = sendChains.get(key);
  if (chain === undefined) {
    chain = { tail: Promise.resolve() };
    sendChains.set(key, chain);
  }
  const priorSend = chain.tail;
  let releaseSend: () => void = () => {};
  const slot = new Promise<void>((resolve) => {
    releaseSend = resolve;
  });
  // Everything entering after us waits on our slot, so the chain stays a strict
  // FIFO of submission order.
  chain.tail = slot;
  // Captured, not re-read: `rekey` may move this chain to another key, and our
  // release must still find the box we actually queued on.
  const ownChain = chain;
  return {
    waitForPrior: async () => {
      // Bounded so a stalled send can't deadlock its successors: `postEvent`
      // has no timeout, so a POST whose connection dies never settles and its
      // link never releases. Past SEND_CHAIN_MAX_WAIT_MS the successor proceeds
      // anyway — only ordering degrades, versus a composer that queues forever.
      let timer: ReturnType<typeof setTimeout> | undefined;
      try {
        await Promise.race([
          priorSend,
          new Promise<void>((resolve) => {
            timer = setTimeout(resolve, SEND_CHAIN_MAX_WAIT_MS);
          }),
        ]);
      } finally {
        if (timer !== undefined) clearTimeout(timer);
      }
    },
    rekey: (resolvedConversationId) => {
      // A brand-new chat's id exists only after `createSession`, so this chain
      // starts under the new-session key. `ensureBoundSession` then publishes
      // that id to the store and awaits its bind — a send issued in that window
      // resolves the real id, and would find an empty chain and overtake us.
      //
      // So this MUST run while the id is still private to the creating call:
      // `ensureBoundSession` invokes it via `onSessionResolved`, immediately
      // after `createSession` and before anything publishes the id.
      //
      // The WHOLE chain moves, not just this send's slot: followers that queued
      // under the new-session key are already waiting on `ownChain.tail`, so
      // re-pointing the box preserves their order behind us. Installing a bare
      // slot at the new key instead would let a send arriving after the id is
      // published wait on US rather than on the true tail, and the two would
      // then POST concurrently and could arrive out of order.
      if (sendChains.get(resolvedConversationId) === ownChain) return;
      // A pre-existing chain under the real id can't happen (the id was just
      // minted), but if it somehow did, splice ours behind it rather than
      // dropping its queued sends on the floor.
      const existing = sendChains.get(resolvedConversationId);
      if (existing !== undefined && existing !== ownChain) {
        const priorTail = existing.tail;
        ownChain.tail = priorTail.then(() => ownChain.tail);
      }
      if (sendChains.get(NEW_SESSION_SEND_CHAIN_KEY) === ownChain) {
        sendChains.delete(NEW_SESSION_SEND_CHAIN_KEY);
      }
      sendChains.set(resolvedConversationId, ownChain);
    },
    releaseSend: () => {
      // Drop the chain when nothing is queued behind us — our slot is still its
      // tail — so the map can't grow without bound across a long session's
      // conversations. A follower would have replaced `tail`, and must keep it.
      if (ownChain.tail === slot) {
        for (const [k, v] of sendChains) {
          if (v === ownChain) sendChains.delete(k);
        }
      }
      releaseSend();
    },
  };
}
let flashTimer: ReturnType<typeof setTimeout> | null = null;
const workspaceInvalidationTimers = new Map<string, ReturnType<typeof setTimeout>>();

// Background-flush throttle, kept OUT of store state so it can't re-trigger the
// queue effect. A conversation currently mid-POST (inFlight) or in its
// post-failure cooldown is skipped, so `flushBackgroundQueues` can't spin into
// a tight retry loop against a persistently-failing idle conversation — a
// failed POST leaves it idle in the cache, which would otherwise re-fire on
// every re-queue. Cooldown paces retries to roughly the sidebar poll cadence.
const BACKGROUND_FLUSH_COOLDOWN_MS = 5_000;
const backgroundFlushInFlight = new Set<string>();
const backgroundFlushCooldownUntil = new Map<string, number>();

// Failure-scoped backoff for the silent sticky-apply PATCHes (effort/model): if
// the backend errors they never persist and would re-fire on every rebind, so a
// failure pauses them and the next success resumes. Reset in initChatStore.
const STICKY_APPLY_BACKOFF_MS = 30_000;
let stickyApplyBackoffUntil = 0;

// Silent sticky applies pause for a cooldown after any failure — treated as a
// transient backend-wide hiccup (a 404 here means the permission check didn't
// succeed, a flaky permission service, not that the session is gone), so one
// failure pauses every session. Explicit /model and /effort picks aren't gated.
function stickyApplyBlocked(): boolean {
  return Date.now() < stickyApplyBackoffUntil;
}

// Arm on failure only; the gate reopens by time, never on a success. During an
// outage the ~10% of requests that succeed must not flap the gate open and leak
// a fresh apply each time.
function armStickyApplyBackoff(): void {
  stickyApplyBackoffUntil = Date.now() + STICKY_APPLY_BACKOFF_MS;
}

// Remembers each File's successful upload so a retry reuses the server-assigned
// file_id instead of re-uploading the blob (which would orphan the prior one).
// Retries re-send the same File objects — background flush re-queues them on a
// cooldown, and any send whose post fails after an upload succeeded — so keying
// by File identity dedupes across attempts. Keyed by session too, since a File
// could be sent to more than one. WeakMap so entries vanish when the File is
// dropped from the queue/pending state.
const uploadedFileBlockCache = new WeakMap<File, Map<string, ContentBlock>>();

/**
 * Upload a file to a session and return its content block, reusing a prior
 * successful upload of the same File to the same session. Deduping here means
 * a failed post (or a later file's upload failing) doesn't re-upload files
 * that already landed when the message is retried.
 */
async function uploadFileBlock(sessionId: string, file: File): Promise<ContentBlock> {
  const cached = uploadedFileBlockCache.get(file)?.get(sessionId);
  if (cached !== undefined) return cached;
  const uploaded = await uploadFile(sessionId, file);
  const block: ContentBlock = file.type.startsWith("image/")
    ? { type: "input_image", file_id: uploaded.id, filename: uploaded.filename }
    : { type: "input_file", file_id: uploaded.id, filename: uploaded.filename };
  let bySession = uploadedFileBlockCache.get(file);
  if (bySession === undefined) {
    bySession = new Map<string, ContentBlock>();
    uploadedFileBlockCache.set(file, bySession);
  }
  bySession.set(sessionId, block);
  return block;
}

async function uploadFileBlocks(
  sessionId: string,
  files: readonly File[],
): Promise<ContentBlock[]> {
  const blocks: ContentBlock[] = [];
  // Stop at the first failure so later uploads cannot outlive a requeued send.
  for (const file of files) {
    // oxlint-disable-next-line no-await-in-loop
    blocks.push(await uploadFileBlock(sessionId, file));
  }
  return blocks;
}

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
// A reverse proxy serves 404 for the stream route for the ~10-60s a backend
// container takes to restart (upgrade, config change, re-seed bounce), so a
// 404 mid-restart must not be treated as permanent. Bound the retries instead
// of trusting them forever, so a truly deleted/invalid conversation still
// gives up rather than polling it forever.
const MAX_TRANSIENT_404_RETRIES = 10;

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

// Bumped by every explicit model pick, so a PATCH that resolves out of order can
// tell whether its canonicalization is still the newest choice. A conversation-id
// check is not enough: the user can pick in A, switch to B and pick again, then
// have A's slower PATCH land last — and two reordered picks WITHIN one
// conversation share an id. Only the newest pick may settle the sticky pref.
let modelPickRevision = 0;

/**
 * Make `id` the live, active conversation and return setters bound to its entry.
 *
 * Test-only seam. Production reaches this state through `switchTo` /
 * `ensureBoundSession`, which also bind a stream and fetch a snapshot; tests
 * usually want the state without the network. Seeding through the root store
 * instead would write to the projection, which the next mirror overwrites.
 *
 * :param id: conversation to bind, or ``null`` for the landing route.
 * :param state: optional initial conversation state.
 * :returns: the entry's `(set, get)` pair, for driving `startStreamPump` and
 *     friends the way production does.
 */
export function bindConversationForTest(
  id: string | null,
  state?: Partial<ConversationState>,
): { set: Setter; get: Getter } {
  useChatStore.setState({ conversationId: id });
  conversationRegistry.setActive(id);
  if (id === null) {
    useChatStore.setState(createInitialConversationState() as Partial<ChatState>);
    return { set: setActive, get: () => useChatStore.getState() };
  }
  const entry = conversationRegistry.acquire(id);
  if (state !== undefined) entry.setState(state);
  mirrorActiveEntry();
  return { set: entrySetter(entry), get: entryGetter(entry) };
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
  backgroundFlushInFlight.clear();
  backgroundFlushCooldownUntil.clear();
  stickyApplyBackoffUntil = 0;
  // Drop every live conversation: their streams must not outlive the app (or,
  // in tests, leak into the next case).
  conversationRegistry.clear();
  // Drop this tab's held stream slots; disposed pumps release their own locks,
  // and a boot/reset starts from an empty set.
  heldStreamSlots.clear();
  // Reset the POST-ordering chains so a prior run's unresolved send can't block
  // the next one (production calls this once at boot; tests call it per case).
  // The send latch is per-conversation state now, cleared with the registry above.
  sendChains.clear();
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

export const useChatStore = create<ChatState>((_rootSet, get) => ({
  conversationId: null,
  redirectToConversationId: null,
  blocks: [],
  pendingUserMessages: [],
  queuedMessages: [],
  activeResponse: null,
  interruptedResponseIds: [],
  status: "idle",
  sessionStatus: "idle",
  backgroundTaskCount: 0,
  backgroundTasks: [],
  blockedOn: null,
  isNativeTerminalSession: false,
  nativeVendorOwnsModel: false,
  boundAgentId: null,
  boundAgentName: null,
  loadingConversation: false,
  conversationLoadError: null,
  selectedEffort: loadPickerPref(PICKER_PREF_EFFORT_KEY),
  selectedModel: loadPickerPref(PICKER_PREF_MODEL_KEY),
  sessionModelOverride: null,
  sessionReasoningEffort: null,
  costControlModeOverride: null,
  subagentRoutingOverride: null,
  codexPlanMode: false,
  claudePermissionMode: "",
  hasMoreHistory: false,
  loadingMoreHistory: false,
  oldestItemId: null,
  flashItemId: null,
  pendingComposerAttachments: [],
  streamBudgetExceeded: false,
  streamBudgetBannerDismissed: false,
  failedSendDraft: null,
  sendLatchedAt: null,
  llmModel: null,
  pendingModelChange: null,
  sessionHarness: null,
  subAgentName: null,
  contextWindow: null,
  tokensUsed: null,
  sessionCostUsd: null,
  sessionUsageByModel: null,
  gitBranch: null,
  todos: [],
  skills: [],
  codexModelOptions: [],
  terminalPending: false,
  runnerLaunchedAt: null,
  viewers: [],
  sandboxStatus: null,
  mcpStartup: null,
  abortController: null,
  historyGeneration: 0,

  enqueueMessage: (text, files) => {
    const { conversationId, boundAgentId } = get();
    if (conversationId === null) return;
    queueSeq += 1;
    const queueId = `q_${queueSeq}`;
    setActive((s) => ({
      queuedMessages: [
        ...s.queuedMessages,
        {
          queueId,
          text,
          conversationId,
          ...(boundAgentId !== null ? { agentId: boundAgentId } : {}),
          ...(files && files.length > 0 ? { files } : {}),
        },
      ],
    }));
    // A message queued while the agent is idle (a race where the send routed
    // to the queue but the turn had already ended) would otherwise wait for an
    // idle edge that never comes — flush now.
    get().maybeFlushQueuedHead();
  },

  dequeueMessage: (queueId) => {
    setActive((s) => ({
      queuedMessages: s.queuedMessages.filter((m) => m.queueId !== queueId),
    }));
  },

  reorderQueuedMessage: (queueId, beforeQueueId) => {
    setActive((s) => {
      const moved = s.queuedMessages.find((m) => m.queueId === queueId);
      if (moved === undefined || queueId === beforeQueueId) return {};
      const conversationId = moved.conversationId;

      // Reorder only within this conversation's messages, in their current
      // relative order, then drop `moved` before its target (or at the end).
      const own = s.queuedMessages.filter((m) => m.conversationId === conversationId);
      const without = own.filter((m) => m.queueId !== queueId);
      const at =
        beforeQueueId === null
          ? without.length
          : without.findIndex((m) => m.queueId === beforeQueueId);
      if (at === -1) return {}; // target isn't in this conversation — no-op
      const reordered = [...without.slice(0, at), moved, ...without.slice(at)];
      if (reordered.every((m, i) => m.queueId === own[i]?.queueId)) return {}; // unchanged

      // Refill this conversation's slots (their absolute positions in the flat
      // array) with the reordered run; other conversations' entries stay put.
      let next = 0;
      return {
        queuedMessages: s.queuedMessages.map((m) =>
          m.conversationId === conversationId ? reordered[next++]! : m,
        ),
      };
    });
  },

  steerMessage: (queueId) => {
    const s = get();
    const target = s.queuedMessages.find((m) => m.queueId === queueId);
    const agentId = target?.agentId ?? s.boundAgentId;
    if (target === undefined || agentId === null) return;
    // Remove BEFORE the POST so a concurrent flush can't also send it.
    setActive({ queuedMessages: s.queuedMessages.filter((m) => m.queueId !== queueId) });
    void s.send(target.text, agentId, target.files);
  },

  clearQueuedMessages: (conversationId) => {
    setActive((s) => {
      if (!s.queuedMessages.some((m) => m.conversationId === conversationId)) return {};
      return {
        queuedMessages: s.queuedMessages.filter((m) => m.conversationId !== conversationId),
      };
    });
  },

  maybeFlushQueuedHead: () => {
    const s = get();
    // Flush once the agent loop is free to take a turn. `waiting` is NOT busy:
    // the turn already ended and only background work (background shells /
    // sub-agents) outlives it, so the server accepts a new turn immediately —
    // mirror `shouldQueueSend`. Only the local send lifecycle (`streaming`) and
    // an actively `running` turn gate the flush. No agent → nothing to send to.
    if (s.conversationId === null || s.boundAgentId === null || s.sessionStatus === "running") {
      return;
    }
    if (s.status === "streaming") {
      // A send owns the latch, so the queue waits for it — that is the
      // one-message-per-turn contract. Unless the latch is stranded, in which
      // case waiting is forever: clear it so the composer also stops queueing
      // (`shouldQueueSend` reads the same flag) and drain below. Only the
      // ACTIVE conversation can wedge like this; `flushBackgroundQueues`
      // already drives every other queue off the server's own status.
      if (!sendLatchIsStranded(s)) return;
      // The stranded send still holds this conversation's chain link, so the
      // drain below would park on it for another SEND_CHAIN_MAX_WAIT_MS. Same
      // evidence, same conclusion: drop the link too. If that send ever does
      // settle, its `release` only clears an entry it still owns, so a fresh
      // chain started here is safe.
      sendChains.delete(s.conversationId);
      // Clear the latch on THIS conversation's entry only, alongside its status.
      setActive({ status: "idle", sendLatchedAt: null });
    }
    // Flush the FIRST message OF THE BOUND CONVERSATION (FIFO within it), not
    // the global array head. The queue is one flat array across conversations,
    // so an undrained message from another conversation can sit at index 0; a
    // head-only guard would let it block this conversation's messages forever.
    const head = s.queuedMessages.find((m) => m.conversationId === s.conversationId);
    if (head === undefined) return;
    // Remove it BEFORE the POST so a re-entrant flush can't double-send.
    setActive({ queuedMessages: s.queuedMessages.filter((m) => m.queueId !== head.queueId) });
    void s.send(head.text, head.agentId ?? s.boundAgentId, head.files);
  },

  flushBackgroundQueues: () => {
    const s = get();
    if (queryClient === null || s.queuedMessages.length === 0) return;

    // Conversations (other than the active one) that have a queued message.
    // The active conversation is owned by maybeFlushQueuedHead.
    const candidateIds = new Set(
      s.queuedMessages.map((m) => m.conversationId).filter((id) => id !== s.conversationId),
    );
    if (candidateIds.size === 0) return;

    // Per-conversation status from the sidebar cache (kept live by the WS
    // /v1/sessions/updates overlay + poll), so we can tell whether a
    // navigated-away conversation is idle without its SSE stream. A conversation
    // scrolled past the loaded pages has no row here → treated as not-idle and
    // left for the foreground flush when the user navigates back to it.
    const statusById = new Map<string, string | undefined>();
    for (const [, data] of queryClient.getQueriesData<ConversationsInfiniteData>({
      queryKey: ["conversations"],
    })) {
      for (const page of data?.pages ?? []) {
        for (const row of page.data) {
          if (candidateIds.has(row.id) && !statusById.has(row.id)) {
            statusById.set(row.id, row.status);
          }
        }
      }
    }

    // One message per idle conversation per call: POSTing makes it busy, so the
    // next idle (via WS/poll) triggers this again for the next message (FIFO).
    const now = Date.now();
    for (const conversationId of candidateIds) {
      if (statusById.get(conversationId) !== "idle") continue;
      // Skip a conversation mid-POST or in its post-failure cooldown so a
      // persistent failure can't spin this into a tight retry loop (the effect
      // re-fires on every re-queue, and a failed POST leaves the row idle).
      if (backgroundFlushInFlight.has(conversationId)) continue;
      const cooldownUntil = backgroundFlushCooldownUntil.get(conversationId);
      if (cooldownUntil !== undefined && cooldownUntil > now) continue;
      const head = get().queuedMessages.find((m) => m.conversationId === conversationId);
      if (head === undefined) continue;

      // Remove BEFORE the work starts so a re-entrant trigger can't double-send.
      backgroundFlushInFlight.add(conversationId);
      setActive((st) => ({
        queuedMessages: st.queuedMessages.filter((m) => m.queueId !== head.queueId),
      }));
      // Join the SAME send chain the foreground path uses for this
      // conversation. A queued message can hand off from the foreground flush
      // (send() → its chain) to here the moment the user navigates away, and
      // the two POST paths would otherwise race — a background postEvent could
      // overtake a foreground send() still awaiting its chain slot, delivering
      // out of FIFO order. Taking a slot here (wait before the upload/post,
      // release in finally) serializes every POST to this conversation across
      // both paths through one ordering primitive.
      const { waitForPrior, releaseSend } = enterSendChain(conversationId);
      // Upload any attachments, then post the message referencing their
      // server-assigned file_ids — the same two-phase sequence send() runs
      // (no combined endpoint exists: /resources/files stores the blob and
      // returns an id, /events posts a message that points at that id). Both
      // awaits sit under the one in-flight guard and the one catch, so a
      // failure in either phase re-queues and backs off together.
      //
      // No optimistic bubble — we're not viewing this conversation; it
      // re-hydrates from the snapshot on return. On failure re-queue at the
      // head (preserving this conversation's FIFO order) and set a cooldown so
      // the next trigger backs off instead of hammering a failing runner.
      void (async () => {
        await waitForPrior();
        // Reuse prior successful uploads so cooldown-paced retries do not
        // orphan blobs that already landed.
        const fileBlocks = await uploadFileBlocks(conversationId, head.files ?? []);
        const content: ContentBlock[] = [
          ...fileBlocks,
          ...(head.text.trim() ? [{ type: "input_text" as const, text: head.text }] : []),
        ];
        await postEvent(conversationId, {
          type: "message",
          data: { role: "user", content },
        });
      })()
        .catch(() => {
          backgroundFlushCooldownUntil.set(
            conversationId,
            Date.now() + BACKGROUND_FLUSH_COOLDOWN_MS,
          );
          setActive((st) => {
            const idx = st.queuedMessages.findIndex((m) => m.conversationId === conversationId);
            const at = idx === -1 ? st.queuedMessages.length : idx;
            return {
              queuedMessages: [
                ...st.queuedMessages.slice(0, at),
                head,
                ...st.queuedMessages.slice(at),
              ],
            };
          });
        })
        .finally(() => {
          backgroundFlushInFlight.delete(conversationId);
          // Hand the chain to the next POST (foreground or background) so it
          // can start its own network work in submission order.
          releaseSend();
        });
    }
  },

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
      // Latch on the SAME entry as `status`, in one patch, so they can't
      // diverge — a new chat buffers both on root and `adoptPreSessionState`
      // moves them onto the entry together.
      setActive({ status: "streaming", activeResponse: null, sendLatchedAt: Date.now() });
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
    setActive((s) => ({
      pendingUserMessages: [
        ...s.pendingUserMessages,
        {
          tempId,
          content,
          createdAtS: Math.floor(Date.now() / 1000),
          ...(selfAuthor !== null ? { author: selfAuthor } : {}),
        },
      ],
      // A new turn does NOT supersede the background-shell tally: shells
      // launched in an earlier turn keep running across the turn boundary, so
      // the composer pill must stay lit alongside the "Working…" shimmer rather
      // than blink off the moment the user sends. The count is sticky (see the
      // `session_status` handler) and the next Stop hook re-reports it
      // authoritatively. Only the parked-dialog reason clears — a fresh send is
      // not parked on a dialog.
      blockedOn: null,
    }));

    // Pin the destination before joining the send chain: a stalled prior
    // send can delay this POST past a session switch, and resolving the
    // target afterward would leak the message into the now-active session.
    const submitConversationId = get().conversationId;

    // Take our place in THIS conversation's send chain: wait for its prior
    // send's network work, then hand off to the next via `releaseSend` in the
    // finally below. This serializes POSTs in submission order without delaying
    // the optimistic bubble rendered above. The wait is bounded (see
    // `enterSendChain`), so a stalled prior send can't queue this one forever.
    const { waitForPrior, rekey, releaseSend } = enterSendChain(submitConversationId);

    // The session this send actually posts to, once resolved. Read in the
    // catch to decide whether a failure may touch the active session's UI.
    let postedSessionId: string | null = null;

    try {
      await waitForPrior();
      // `rekey` runs INSIDE the call, the moment `createSession` returns and
      // before the new id is published — a send issued during the bind would
      // otherwise resolve that id, find an empty chain, and overtake this POST.
      const sessionId = await ensureBoundSession(agentId, get, opts, submitConversationId, rekey);
      postedSessionId = sessionId;

      // Upload any attached files and build the real content blocks with
      // server-assigned file_ids (input_image for images, input_file
      // otherwise). Plain text (if any) appended last. uploadFileBlock reuses
      // a prior successful upload of the same File so a retry after a
      // post-phase failure doesn't re-upload — and orphan — blobs that landed.
      const fileBlocks = await uploadFileBlocks(sessionId, files ?? []);
      const serverContent: ContentBlock[] = [
        ...fileBlocks,
        ...(text.trim() ? [{ type: "input_text" as const, text }] : []),
      ];

      // Promote "pending:<filename>" to real file_ids. Claude-native's
      // session.input.consumed is text-only (transcript round-trip
      // drops input_image blocks), so the consumed handler falls back
      // to the pending file blocks — they must already carry real ids.
      //
      // Targets the session this send posted to, not the visible one: the
      // upload can outlive a switch away, and leaving the bubble on
      // `pending:<filename>` ids means the consumed fallback commits those
      // placeholders as if they were real attachments.
      if (fileBlocks.length > 0) {
        setterFor(sessionId)((s) => ({
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
        // Target the session this send posted to: the user may have navigated
        // away while the POST was open, and settling the VISIBLE conversation
        // would clobber an unrelated chat's composer state.
        setterFor(sessionId)((s) => {
          const patch: Partial<ChatState> = {
            pendingUserMessages: s.pendingUserMessages.filter((p) => p.tempId !== tempId),
          };
          if (!alreadyStreaming) {
            patch.status = "idle";
            patch.sessionStatus = "idle";
            patch.backgroundTaskCount = 0;
            patch.backgroundTasks = [];
          }
          return patch;
        });
      } else {
        // POST accepted: the server can now account for this message
        // (native: pending_inputs replay until the round-trip commits it;
        // non-native: already persisted). Mark the bubble settled — that is
        // what releases this conversation's entry for eviction, since the
        // server can now replay it. The bubble keeps rendering until its
        // consumed event pops it.
        setterFor(sessionId)((s) => ({
          pendingUserMessages: s.pendingUserMessages.map((p) =>
            p.tempId === tempId ? { ...p, posted: true } : p,
          ),
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
      // Hand the failed message back to the composer so the user can retry it —
      // a failed send has no server-side record, so nothing else would restore
      // it. Keyed by the session it was meant for, so it lands in the right
      // composer even after a switch away. Written to that conversation's entry
      // (`failedSendDraft` is conversation-scoped); the composer reads whichever
      // conversation is active and guards on the id before restoring.
      const draftSessionId = postedSessionId ?? submitConversationId;
      if (draftSessionId !== null && (text.trim() !== "" || (files?.length ?? 0) > 0)) {
        setterFor(draftSessionId)({
          failedSendDraft: { conversationId: draftSessionId, text, files: files ?? [] },
        });
      }
      // Settle the conversation this send targeted, wherever the user is now:
      // its bubble must roll back and its status must not stay "streaming"
      // forever. When the throw came from session setup itself
      // (`postedSessionId` never resolved) there is no target conversation, so
      // it belongs to the active one — the landing composer's own failure.
      const failSet = postedSessionId === null ? setActive : setterFor(postedSessionId);
      const failGet = (): ChatState =>
        postedSessionId === null ? get() : (setterForState(postedSessionId) ?? get());
      // Roll back the optimistic bubble — no server idle will fire.
      failSet((s) => ({
        pendingUserMessages: s.pendingUserMessages.filter((p) => p.tempId !== tempId),
      }));
      if (!alreadyStreaming) {
        if (failGet().activeResponse !== null) {
          // A response bubble already exists (the turn started, then failed)
          // — mark it failed so the error rides on that bubble.
          finalizeActive(failSet, "failed", message, null);
        } else {
          // No response bubble to carry the failure — the turn never started
          // (e.g. the runner never came online, so POST /events 503'd). Append
          // a standalone error block so the user sees WHY nothing happened
          // instead of being left on a silent, empty composer.
          failSet((s) => ({ blocks: [...s.blocks, makeClientErrorBlock(message, code)] }));
        }
        failSet({
          status: "idle",
          sessionStatus: "idle",
          backgroundTaskCount: 0,
          backgroundTasks: [],
        });
      } else {
        // Sent alongside an already-streaming turn (or a stranded latch): the
        // bubble is rolled back above, so without a block the message vanishes
        // with no trace — the failure mode that makes this class of bug so hard
        // to see. Surface it WITHOUT touching the turn lifecycle: finalizeActive
        // would fail a live response, and settling status would end a turn that
        // is still running.
        failSet((s) => ({ blocks: [...s.blocks, makeClientErrorBlock(message, code)] }));
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
      // See `send`: latch and status on one entry, in one patch.
      setActive({ status: "streaming", activeResponse: null, sendLatchedAt: Date.now() });
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
    setActive((s) => ({
      pendingUserMessages: [
        ...s.pendingUserMessages,
        {
          tempId,
          content: [{ type: "input_text" as const, text: commandText }],
          createdAtS: Math.floor(Date.now() / 1000),
          ...(selfAuthor !== null ? { author: selfAuthor } : {}),
        },
      ],
    }));

    // Pin the destination at submit time — see `send` above for why a late
    // resolve mis-routes to the session the user has since switched to.
    const submitConversationId = get().conversationId;

    const { waitForPrior, rekey, releaseSend } = enterSendChain(submitConversationId);

    // The session this command actually posts to, once resolved.
    let postedSessionId: string | null = null;

    try {
      await waitForPrior();
      // See `send`: rekey inside the call, before the new id is visible.
      const sessionId = await ensureBoundSession(agentId, get, opts, submitConversationId, rekey);
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
        // optimistic echo — roll it back here alongside the status settle.
        // Targets the session the command posted to: a backgrounded
        // conversation must still roll its echo back, and settling the VISIBLE
        // one would clobber an unrelated chat.
        setterFor(sessionId)((s) => {
          const patch: Partial<ChatState> = {
            pendingUserMessages: s.pendingUserMessages.filter((p) => p.tempId !== tempId),
          };
          if (!alreadyStreaming) {
            patch.status = "idle";
            patch.sessionStatus = "idle";
            patch.backgroundTaskCount = 0;
            patch.backgroundTasks = [];
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
        setterFor(sessionId)((s) => ({
          pendingUserMessages: s.pendingUserMessages.map((p) =>
            p.tempId === tempId ? { ...p, posted: true } : p,
          ),
        }));
      }
      queryClient?.invalidateQueries({ queryKey: ["conversations"] });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      // Settle the conversation this command targeted, wherever the user is
      // now: its echo must roll back and its status must not stay "streaming"
      // forever. A throw from session setup itself (`postedSessionId` never
      // resolved) has no target conversation, so it belongs to the active one —
      // the landing composer's own failure. Mirrors `send`'s catch.
      const failSet = postedSessionId === null ? setActive : setterFor(postedSessionId);
      // Roll back the optimistic echo — no receipt will reconcile it.
      failSet((s) => ({
        pendingUserMessages: s.pendingUserMessages.filter((p) => p.tempId !== tempId),
      }));
      if (!alreadyStreaming) {
        finalizeActive(failSet, "failed", message, null);
        failSet({ status: "idle" });
      } else {
        // Same as `send`: surface the failure without settling a turn that may
        // still be live, so a failed command can't vanish silently.
        const { code } = describeSendFailure(err);
        failSet((s) => ({ blocks: [...s.blocks, makeClientErrorBlock(message, code)] }));
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
    setActive((s) => {
      if (s.conversationId !== sessionId) return {};
      const patch: Partial<ChatState> = {
        pendingUserMessages: [],
        status: "idle",
        sessionStatus: "idle",
        backgroundTaskCount: 0,
        backgroundTasks: [],
        blockedOn: null,
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

    // Whether this conversation is already live AND current decides everything
    // below, so read it before anything can create the entry. A retained entry
    // whose stream died (terminal status, failed bind) is deliberately NOT
    // treated as live: it must cold-bind again or it stays stale forever.
    const wasLive = conversationId !== null && isConversationStreamCurrent(conversationId);

    // No abort, no state wipe: the outgoing conversation's entry keeps its
    // stream open and keeps applying events in the background. That is the
    // whole feature — returning to it paints instantly and is already current,
    // instead of paying a reconnect plus a snapshot re-fetch.
    rootSetState({
      conversationId,
      // Clear any pending supersession redirect: we've now switched sessions,
      // so a leftover target (e.g. already consumed by the navigate that
      // brought us here) must not fire again.
      redirectToConversationId: null,
      // Drop any queued "Attach to agent" chip the outgoing composer hadn't
      // drained yet, so it can't bleed into the incoming composer (which drains
      // the store on mount).
      pendingComposerAttachments: [],
    });
    conversationRegistry.setActive(conversationId);

    if (conversationId === null) {
      // Landing route: nothing to project, so reset the mirrored fields to a
      // clean slate rather than leaving the last conversation painted.
      rootSetState(createInitialConversationState() as Parameters<typeof rootSetState>[0]);
      return;
    }

    // Sends the server hasn't acknowledged, carried across the re-bind below.
    let unsentOnRebind: PendingUserMessage[] = [];
    if (!wasLive) {
      // Drop any retained-but-dead entry so `acquire` builds a fresh one. A
      // re-bind has to start from the initial state: `bindStream` PREPENDS its
      // snapshot to whatever `blocks` already holds, so re-binding onto a dead
      // entry's stale transcript would duplicate and mis-order it. Unsent
      // bubbles are the one thing worth keeping — the server can't replay what
      // it was never told about.
      unsentOnRebind =
        conversationRegistry
          .peek(conversationId)
          ?.getState()
          .pendingUserMessages.filter((p) => p.posted !== true) ?? [];
      conversationRegistry.release(conversationId);
    }
    const entry = conversationRegistry.acquire(conversationId);
    if (!wasLive) {
      // Cold entry: nothing is hydrated yet, so the page must show the
      // hydrating placeholder rather than mount the composer against empty
      // state. Without this the composer resolves its model label from the
      // sticky cross-session pick (session-scoped fields are still null) and
      // paints the PREVIOUS conversation's model until the snapshot lands.
      // A live entry deliberately skips this — painting it instantly, with no
      // placeholder, is the whole point of keeping streams open.
      entry.setState({
        loadingConversation: true,
        ...(unsentOnRebind.length > 0 ? { pendingUserMessages: unsentOnRebind } : {}),
      });
    }
    // Paint whatever the entry already holds. For a live entry that is the
    // current transcript; for a fresh one it is the initial state.
    mirrorActiveEntry();
    if (wasLive) return;

    // Cold entry: bind its stream and hydrate history. `hydratePending` replays
    // the snapshot's un-consumed native messages — correct here because a fresh
    // entry has no live optimistic bubbles to overwrite.
    await bindStream(conversationId, entrySetter(entry), entryGetter(entry), true);
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
    setActive((s) => ({
      blocks: s.blocks.map((b) =>
        b.type === "elicitation" && b.elicitationId === elicitationId
          ? { ...b, status: "responded", response: responseValue }
          : b,
      ),
    }));
    // Human-in-the-loop decision: accept = granted, decline = rejected, cancel =
    // dismissed without deciding. Fires on the user's action (live only).
    emitInteractionPhase({
      interactionId: elicitationId,
      interactionKind: "approval",
      phase: "complete",
      status: action === "accept" ? "success" : action === "decline" ? "failure" : "cancelled",
    });
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
      //
      // Targets the conversation whose card this is: the approval POST can
      // outlive a switch away, and rolling back the VISIBLE conversation would
      // reopen an unrelated chat's card while leaving this one wrongly
      // answered.
      setterFor(sessionId)((s) => ({
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
    setActive({ flashItemId: itemId });
    flashTimer = setTimeout(() => {
      flashTimer = null;
      setActive({ flashItemId: null });
    }, FLASH_DURATION_MS);
  },

  addComposerAttachment: (attachment) => {
    setActive((s) => {
      const k = composerAttachmentKey(attachment);
      if (s.pendingComposerAttachments.some((a) => composerAttachmentKey(a) === k)) return s;
      return { pendingComposerAttachments: [...s.pendingComposerAttachments, attachment] };
    });
  },

  clearPendingComposerAttachments: () => setActive({ pendingComposerAttachments: [] }),

  dismissStreamBudgetBanner: () => rootSetState({ streamBudgetBannerDismissed: true }),

  markRunnerLaunched: () => setActive({ runnerLaunchedAt: Date.now() }),

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
    // `selectedEffort` is the cross-session sticky pick; `sessionReasoningEffort`
    // is this conversation's effective value. An explicit pick sets both.
    setActive({ selectedEffort: effort, sessionReasoningEffort: effort });
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
      // Harness has no effort control: undo the optimistic session-scoped write
      // so this conversation doesn't claim an effort the server will never hold.
      if (!supportsEffortControl(session)) {
        setterFor(conversationId)({ sessionReasoningEffort: null });
        return;
      }
      await updateSession(conversationId, { reasoningEffort: effort });
    }
  },

  setModel: async (model, opts) => {
    // `selectedModel` is the sticky pick; `sessionModelOverride` is this
    // session's applied override. An explicit `/model` sets both.
    modelPickRevision += 1;
    const pickRevision = modelPickRevision;
    setActive({ selectedModel: model, sessionModelOverride: model });
    savePickerPref(PICKER_PREF_MODEL_KEY, model);
    const { conversationId } = get();
    if (conversationId) {
      const expectConfirmation = opts?.expectConfirmation === true && model !== null;
      if (expectConfirmation) {
        // Reported-model sessions: the chip keeps the harness's model until
        // its own report confirms the switch. Mark the ask pending BEFORE
        // the PATCH: the PATCH is held open while the runner drives and
        // CONFIRMS the switch, so the harness's `session.model` report
        // usually arrives before the PATCH resolves — pending set after the
        // response would miss that clear and strand the spinner. The
        // `session.model` event or a `model_change_not_applied` error
        // settles it; the timer is spinner hygiene for the case where
        // neither ever arrives (e.g. the session fell asleep mid-ask) — it
        // outlasts the server's 20 s runner-forward budget.
        setterFor(conversationId)({ pendingModelChange: model });
        setTimeout(() => {
          setterFor(conversationId)((s) =>
            s.pendingModelChange === model ? { pendingModelChange: null } : {},
          );
        }, 30_000);
      }
      let session;
      try {
        session = await updateSession(conversationId, { modelOverride: model });
      } catch (err) {
        // The ask never reached the server — nothing will confirm it.
        if (expectConfirmation) {
          setterFor(conversationId)((s) =>
            s.pendingModelChange === model ? { pendingModelChange: null } : {},
          );
        }
        throw err;
      }
      // Server-canonical may differ from the optimistic write (e.g.
      // when a clear alias was sent) — refresh local state to match.
      const canonical = session.modelOverride ?? null;
      // The override belongs to the session that was PATCHed, so apply it there
      // even if the user has since switched away.
      setterFor(conversationId)({ sessionModelOverride: canonical });
      // The sticky pref (root + localStorage) is app-global and must reflect the
      // NEWEST pick, so a slower PATCH that resolves last cannot overwrite it —
      // otherwise the superseded model returns on reload or in a new chat. Both
      // writes are gated together: persisting without the root write would leave
      // them disagreeing until the next reload.
      if (pickRevision === modelPickRevision) {
        rootSetState({ selectedModel: canonical });
        savePickerPref(PICKER_PREF_MODEL_KEY, canonical);
      }
    }
  },

  setCostControlMode: async (mode) => {
    const { conversationId } = get();
    if (!conversationId) return;
    const previous = get().costControlModeOverride;
    // Routing and a pinned model are mutually exclusive: the server's routing
    // guard skips whenever model_override is set, so turning routing ON must
    // also clear this session's pinned model (in the SAME PATCH) — otherwise
    // the old pick (e.g. Opus from the new-chat picker) would win and the judge
    // would never run. Mirrors the new-chat dialog's mutual exclusion. Only
    // clear when a model is actually pinned, so toggling routing on a
    // model-less (e.g. SDK) session doesn't emit a spurious model-cleared change.
    const previousModel = get().sessionModelOverride;
    const clearModel = mode === "on" && previousModel != null;
    // Pin the target: these are this conversation's switches, and the PATCH can
    // outlive a switch away. Resolving the target afterwards would leave a
    // backgrounded conversation showing an optimistic value the server rejected,
    // with no re-bind on return to correct it.
    const patchSet = setterFor(conversationId);
    // Optimistic flip so the pill responds instantly; the PATCH
    // response (or the rollback below) is the settled truth.
    patchSet({
      costControlModeOverride: mode,
      ...(clearModel ? { sessionModelOverride: null } : {}),
    });
    try {
      const session = await updateSession(conversationId, {
        costControlModeOverride: mode,
        ...(clearModel ? { modelOverride: null } : {}),
      });
      patchSet({
        costControlModeOverride: session.costControlModeOverride ?? null,
        ...(clearModel ? { sessionModelOverride: session.modelOverride ?? null } : {}),
      });
    } catch (err) {
      // Roll back so neither control claims a state the server never persisted.
      // A disposed entry makes this a no-op, which is the only case worth
      // skipping — there is no state left to correct.
      patchSet({
        costControlModeOverride: previous,
        ...(clearModel ? { sessionModelOverride: previousModel } : {}),
      });
      throw err;
    }
  },

  setSubagentRouting: async (mode) => {
    const { conversationId } = get();
    if (!conversationId) return;
    const previous = get().subagentRoutingOverride;
    // Optimistic write so the select responds instantly; the PATCH response
    // (or the rollback below) is the settled truth. Unlike the session's own
    // routing switch this never touches `model_override` — it governs the
    // sub-agents' models, not this session's.
    // Pinned for the same reason as `setCostControlMode` — see there.
    const patchSet = setterFor(conversationId);
    patchSet({ subagentRoutingOverride: mode });
    try {
      const session = await updateSession(conversationId, { subagentRoutingOverride: mode });
      patchSet({ subagentRoutingOverride: session.subagentRoutingOverride ?? null });
    } catch (err) {
      patchSet({ subagentRoutingOverride: previous });
      throw err;
    }
  },

  refreshSessionOverrides: async () => {
    const { conversationId } = get();
    if (!conversationId) return;
    let session: Session;
    try {
      // Deliberately NOT through `queryClient` — the two switches this reads
      // are plain DB columns, but writing the reply into the shared
      // ``["session", id]`` cache would replace the refreshed snapshot every
      // other surface reads with one the server did not refresh, dropping the
      // runner-backed `model_options` the model picker renders from.
      session = await getSessionSlim(conversationId);
    } catch {
      // Transient (server/runner blip) — keep what we have rather than
      // resetting the switches to their defaults.
      return;
    }
    if (get().conversationId !== conversationId) return;
    setActive({
      costControlModeOverride: session.costControlModeOverride ?? null,
      subagentRoutingOverride: session.subagentRoutingOverride ?? null,
    });
  },

  setCodexPlanMode: async (enabled) => {
    const { conversationId } = get();
    if (!conversationId) return;
    const previous = get().codexPlanMode;
    // Pinned for the same reason as `setCostControlMode` — see there.
    const patchSet = setterFor(conversationId);
    patchSet({ codexPlanMode: enabled });
    try {
      const session = await updateSession(conversationId, { codexPlanMode: enabled });
      patchSet({ codexPlanMode: codexPlanModeFromSession(session) });
    } catch (err) {
      patchSet({ codexPlanMode: previous });
      throw err;
    }
  },

  setClaudePermissionMode: async (mode) => {
    const { conversationId } = get();
    if (!conversationId) return;
    const previous = get().claudePermissionMode;
    // Pinned for the same reason as `setCostControlMode` — see there.
    const patchSet = setterFor(conversationId);
    // Optimistic, then reconciled against the mode the server confirms the
    // pane landed on — which may differ from what was asked.
    patchSet({ claudePermissionMode: mode });
    try {
      const session = await updateSession(conversationId, { claudePermissionMode: mode });
      patchSet({ claudePermissionMode: claudePermissionModeFromSession(session) ?? "" });
    } catch (err) {
      patchSet({ claudePermissionMode: previous });
      throw err;
    }
  },

  loadMoreHistory: async () => {
    const { conversationId, oldestItemId, loadingMoreHistory, hasMoreHistory, historyGeneration } =
      get();
    if (!conversationId || !oldestItemId || loadingMoreHistory || !hasMoreHistory) return;
    // Pin the originating conversation: this page belongs to it, and it may be
    // backgrounded before the fetch lands. Resolving the target afterwards would
    // strand `loadingMoreHistory: true` on it — and since a live entry is never
    // re-bound on return, that guard would then no-op every future scroll-up.
    const pageSet = setterFor(conversationId);
    pageSet({ loadingMoreHistory: true });
    // Drop the result if the window was reset while this page was in flight
    // (navigate away-and-back, rebind hydration, reconnect re-hydrate): the
    // page is cursor-relative to the OLD window, and prepending it into the
    // new one would invert order or rewind the cursor past a silent gap.
    //
    // Backgrounding is NOT staleness — only disposal (the entry is gone, so
    // nothing to fix) or a generation bump (the window moved under us).
    const stale = (): boolean =>
      isConversationDisposed(conversationId) ||
      (setterForState(conversationId)?.historyGeneration ?? historyGeneration) !==
        historyGeneration;
    try {
      const { items, hasMore } = await fetchSessionItemsPage(conversationId, {
        olderThan: oldestItemId,
      });
      if (stale()) return;
      const newBlocks = itemsToBlocks(items);
      pageSet((state) => {
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
      pageSet({ loadingMoreHistory: false, hasMoreHistory: false });
    }
  },
}));

// ── Store-action setter ──────────────────────────────────
//
// The store's own actions (`send`, `stop`, the picker setters, `loadMoreHistory`)
// write a mix of conversation-scoped and app-global state, and they all mean
// "the conversation on screen". Route their patches so conversation keys reach
// that conversation's entry — which is where the state actually lives — and
// app-global keys stay on the root store.
//
// Declared after the store because it closes over `useChatStore`; hoisting makes
// it available to the actions above, which only ever run after module init.
// zustand's own `set` (`_rootSet`) is unused: it writes the root store, which is
// a projection of the active entry, so a write there would be overwritten by the
// next mirror.
function setActive(partial: Partial<ChatState> | ((state: ChatState) => Partial<ChatState>)): void {
  const active = conversationRegistry.getActive();
  if (active === null) {
    // Landing route (`/`): no entry exists yet because the session hasn't been
    // created. Buffer conversation-scoped writes on the root store so the
    // landing composer's optimistic bubble renders immediately; `switchTo` /
    // `ensureBoundSession` adopt them into the new entry once it exists (see
    // `adoptPreSessionState`). Dropping them here would make the first message
    // of a new chat vanish until the server echoed it back.
    rootSetState(partial as Parameters<typeof rootSetState>[0]);
    return;
  }
  entrySetter(active)(partial);
}

// ── Internal helpers ─────────────────────────────────────

type Setter = (partial: Partial<ChatState> | ((state: ChatState) => Partial<ChatState>)) => void;
type Getter = () => ChatState;

// zustand's unwrapped setter, captured before the routing wrapper below
// replaces it. Writes the root store's own fields with no re-splitting — used by
// the mirror and by the app-global half of a routed patch.
const rootSetState = useChatStore.setState;

// ── Origin-wide stream slots ─────────────────────────────
//
// One held slot == one live stream this tab is counted for against the shared,
// cross-tab cap (see `streamSlots`). Keyed by conversation id.
const heldStreamSlots = new Map<string, StreamSlot>();

/**
 * Take an origin-wide stream slot for `id` before opening its stream.
 *
 * Tries for a free slot; if the origin is saturated, reclaims one of THIS tab's
 * own background streams (LRU, unpinned) and retries — awaiting the reclaimed
 * slot's release so the freed lock is observable before the re-check, rather
 * than racing it and over-evicting. Returns whether a slot is now held.
 *
 * Returns false only when a fresh tab finds every slot held by OTHER tabs and
 * has nothing of its own to reclaim; the active conversation then opens over
 * budget (the caller proceeds anyway) and the too-many-tabs banner is raised.
 */
async function acquireStreamSlot(id: string): Promise<boolean> {
  if (heldStreamSlots.has(id)) return true; // rebinding a still-slotted stream
  let slot = await getStreamSlotManager().tryAcquire();
  // Inherently sequential: each iteration must fully release a reclaimed slot
  // (so the freed lock is observable) before re-checking, or we'd over-evict.
  /* eslint-disable no-await-in-loop */
  while (slot === null) {
    const evictedId = conversationRegistry.evictLruEvictable(id);
    if (evictedId === null) break;
    const evictedSlot = heldStreamSlots.get(evictedId);
    if (evictedSlot !== undefined) {
      heldStreamSlots.delete(evictedId);
      await evictedSlot.release();
    }
    slot = await getStreamSlotManager().tryAcquire();
  }
  /* eslint-enable no-await-in-loop */
  if (slot !== null) heldStreamSlots.set(id, slot);
  setStreamBudgetExceeded(slot === null);
  return slot !== null;
}

/** Hand back `id`'s stream slot when its stream ends (pump exits / disposed). */
function releaseStreamSlot(id: string): void {
  const slot = heldStreamSlots.get(id);
  if (slot === undefined) return;
  heldStreamSlots.delete(id);
  void slot.release();
}

/**
 * Raise or clear the too-many-tabs banner. A fresh over-budget episode
 * (false→true) un-dismisses it; clearing leaves the dismissed flag alone since
 * the banner is hidden while within budget regardless.
 */
function setStreamBudgetExceeded(exceeded: boolean): void {
  if (useChatStore.getState().streamBudgetExceeded === exceeded) return;
  rootSetState(
    exceeded
      ? { streamBudgetExceeded: true, streamBudgetBannerDismissed: false }
      : { streamBudgetExceeded: false },
  );
}

// ── Conversation entries ─────────────────────────────────
//
// The streaming machinery (`bindStream`, `startStreamPump`, `pumpStreamEvents`,
// the reconcile helpers) is already threaded on `(set, get)`, so pointing it at
// a conversation entry instead of the root store is a matter of handing it a
// different pair. These build that pair.
//
// Reads see the entry's own state merged under the root store's app-global
// fields and actions, so existing code that reads e.g. `get().selectedEffort`
// or calls `get().send(...)` keeps working. Writes are split by key: conversation
// state goes to the entry, app-global state to the root store.

/**
 * Whether a conversation is no longer live — evicted, released, or never bound.
 *
 * This is the **liveness** check the streaming machinery runs to decide whether
 * to keep pumping, reconnecting, and writing. It replaced
 * `get().conversationId !== id`, which conflated "still loaded?" with "on
 * screen?" — the same question only while one conversation could be open, and
 * the reason a background pump used to exit at its first reconnect check.
 *
 * Being backgrounded is explicitly NOT a reason to stop: a background stream
 * must keep applying events and must keep reconciling across the ingress'
 * ~5-minute stream recycle, or it goes silently stale.
 */
function isConversationDisposed(id: string): boolean {
  const entry = conversationRegistry.peek(id);
  return entry === undefined || entry.disposed;
}

/**
 * Whether a live entry is actually still current — stream open, snapshot loaded.
 *
 * Registry membership alone does NOT mean "up to date". `startStreamPump` clears
 * `abortController` when it stops for good (a terminal 401/403/404, or `[DONE]`),
 * and a failed `bindStream` leaves `conversationLoadError` set; neither releases
 * the entry. Treating those as live means returning to the conversation paints
 * whatever it held when the stream died and never reopens it — stale until the
 * next send. `switchTo` rebinds when this is false; `ensureBoundSession` makes
 * the same check before POSTing.
 */
function isConversationStreamCurrent(id: string): boolean {
  const entry = conversationRegistry.peek(id);
  if (entry === undefined || entry.disposed) return false;
  const state = entry.getState();
  return state.abortController !== null && state.conversationLoadError === null;
}

/**
 * Tear down an entry's stream, keeping the entry itself alive.
 *
 * Needed before re-binding a live entry: a failed snapshot leaves
 * `conversationLoadError` set while its pump is still running (`bindStream`
 * catches the error without aborting), so binding again would strand the old
 * pump — two subscribers on one entry, double-applying any delta that carries no
 * item id to dedupe on. Aborting ends the reconnect loop and cancels the
 * in-flight fetch; `bindStream` installs the replacement controller.
 */
function abortConversationStream(entry: ConversationEntry): void {
  const { abortController } = entry.getState();
  if (abortController === null) return;
  abortController.abort();
  entry.setState({ abortController: null });
}

/** A conversation's state if it is still live, else `null`. */
function setterForState(conversationId: string): ChatState | null {
  const entry = conversationRegistry.peek(conversationId);
  return entry === undefined ? null : entryGetter(entry)();
}

/**
 * A setter for a specific conversation, or a no-op when it is no longer live.
 *
 * Late-settling send work (a denied POST, a failure) must land on the
 * conversation it was sent to — not on whatever the user has since switched to.
 * `setActive` would write the visible conversation, which is how a stale
 * failure could clobber an unrelated chat's composer state.
 */
function setterFor(conversationId: string | null): Setter {
  if (conversationId === null) return () => {};
  const entry = conversationRegistry.peek(conversationId);
  if (entry === undefined) return () => {};
  return entrySetter(entry);
}

/**
 * Move conversation state written before a session existed onto its new entry.
 *
 * The landing composer renders an optimistic bubble the moment the user hits
 * send, which is before `createSession` returns — so those writes land on the
 * root store (see `setActive`). This hands them to the entry, so the bubble
 * survives the transition instead of being replaced by the entry's empty
 * initial state.
 */
function adoptPreSessionState(entry: ConversationEntry): void {
  const root = useChatStore.getState() as unknown as Record<string, unknown>;
  const buffered: Record<string, unknown> = {};
  const initial = createInitialConversationState() as unknown as Record<string, unknown>;
  for (const key of Object.keys(initial)) {
    const value = root[key];
    // Only carry values that actually differ from a cold start, so an unrelated
    // field can't be pinned to whatever the previous conversation left behind.
    if (value !== undefined && value !== initial[key]) buffered[key] = value;
  }
  if (Object.keys(buffered).length > 0) {
    entry.setState(buffered as Partial<ConversationState>);
  }
}

/** Read an entry's conversation state as if it were the whole `ChatState`. */
function entryGetter(entry: ConversationEntry): Getter {
  return () =>
    ({
      ...useChatStore.getState(),
      ...entry.getState(),
      // The root's `conversationId` names what's ON SCREEN. For an entry it
      // must name the entry itself, so code reading `get().conversationId`
      // sees the conversation it is working on, not the one being viewed.
      conversationId: entry.id,
    }) as ChatState;
}

/**
 * Write to an entry, routing app-global keys to the root store.
 *
 * The split is by key rather than by caller because the streaming code writes
 * mixed patches (e.g. `bindStream` sets conversation fields alongside the
 * sticky-pref handoff). `conversationId` is dropped: an entry's identity is
 * fixed, and letting a patch move it would silently retarget the stream.
 */
function entrySetter(entry: ConversationEntry): Setter {
  return (partial) => {
    const patch = typeof partial === "function" ? partial(entryGetter(entry)()) : partial;
    const conversationPatch: Record<string, unknown> = {};
    const appPatch: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(patch)) {
      if (key === "conversationId") continue;
      if (isConversationStateKey(key)) conversationPatch[key] = value;
      else appPatch[key] = value;
    }
    if (Object.keys(appPatch).length > 0) {
      // Root store directly: this half is app-global by construction, and going
      // through the routing wrapper would re-split it.
      rootSetState(appPatch as unknown as Parameters<typeof rootSetState>[0]);
    }
    if (Object.keys(conversationPatch).length > 0) {
      entry.setState(conversationPatch as Partial<ConversationState>);
    }
  };
}

/**
 * Project an entry's state onto the root store so components keep reading flat
 * fields (`useChatStore((s) => s.blocks)`) without knowing about entries.
 *
 * One-directional by design — entry → root, never back. A bidirectional mirror
 * would make "which copy is the truth" ambiguous the moment they disagreed.
 */
function mirrorActiveEntry(): void {
  const activeId = useChatStore.getState().conversationId;
  if (activeId === null) return;
  const entry = conversationRegistry.peek(activeId);
  if (entry === undefined) return;
  rootSetState(entry.getState() as Parameters<typeof rootSetState>[0]);
}

// Route `useChatStore.setState` so a conversation-scoped write reaches the
// active entry rather than the root store's projection of it.
//
// Without this, a caller that reaches for the store directly — the load-test
// harness, the test suite's ~230 state seeds — would write to the projection and
// have it silently overwritten by the next mirror. Wrapping the store's own
// setter keeps `useChatStore.setState({ blocks })` meaning what it always meant:
// "set the visible conversation's blocks".
useChatStore.setState = ((partial: unknown, replace?: boolean) => {
  const patch =
    typeof partial === "function"
      ? (partial as (s: ChatState) => Partial<ChatState>)(useChatStore.getState())
      : (partial as Partial<ChatState>);
  // A patch that names a conversation makes it the active one, binding an entry
  // if needed. Production reaches this through `switchTo`; this covers the
  // direct-store callers (the load-test harness, the test suite's state seeds),
  // for which "set conversationId and some blocks" has always meant "make this
  // the conversation being viewed".
  if ("conversationId" in patch) {
    const nextId = patch.conversationId ?? null;
    if (nextId !== useChatStore.getState().conversationId) {
      rootSetState({ conversationId: nextId } as Parameters<typeof rootSetState>[0]);
      conversationRegistry.setActive(nextId);
      if (nextId !== null) conversationRegistry.acquire(nextId);
    }
  }
  const active = conversationRegistry.getActive();
  if (active === null || replace === true) {
    return rootSetState(partial as Parameters<typeof rootSetState>[0], replace as never);
  }
  const conversationPatch: Record<string, unknown> = {};
  const appPatch: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(patch)) {
    if (isConversationStateKey(key)) conversationPatch[key] = value;
    else appPatch[key] = value;
  }
  if (Object.keys(appPatch).length > 0) {
    rootSetState(appPatch as unknown as Parameters<typeof rootSetState>[0]);
  }
  if (Object.keys(conversationPatch).length > 0) {
    // The entry's change notification mirrors this back onto the root store.
    active.setState(conversationPatch as Partial<ConversationState>);
  }
}) as typeof useChatStore.setState;

// Keep the root store's flat fields in step with whichever entry is on screen.
conversationRegistry.subscribe((id) => {
  if (useChatStore.getState().conversationId !== id) return;
  const state = conversationRegistry.peek(id)?.getState();
  if (state !== undefined) rootSetState(state as Parameters<typeof rootSetState>[0]);
});

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
 * :param onSessionResolved: Fires with the new session id the instant
 *     ``createSession`` returns — BEFORE the id is published to the store, so
 *     the caller can move its send-chain slot onto the real id before any other
 *     send can resolve that id and key an empty chain. See `enterSendChain`.
 * :returns: The bound session id.
 * :raises Error: Re-raises a ``conversationLoadError`` if a needed rebind
 *     of an existing session fails to establish the stream.
 */
async function ensureBoundSession(
  agentId: string,
  get: Getter,
  opts?: SendOptions,
  pinnedConversationId?: string | null,
  onSessionResolved?: (sessionId: string) => void,
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
    // Claim the send chain for this id NOW, while it is still private to this
    // call. Everything below publishes it (the store write, the navigate
    // callback, the sidebar invalidation) and awaits network work, so a send
    // issued from here on resolves this id — and would find an empty chain and
    // overtake us if our slot were still under the new-session key.
    onSessionResolved?.(sessionId);
    // Native runners read reasoning_effort during bind.
    const preBindEffort = useChatStore.getState().selectedEffort;
    if (preBindEffort != null && supportsEffortControl(session)) {
      await updateSession(sessionId, {
        reasoningEffort: preBindEffort,
        silent: true,
      });
    }
    await bindOnlyOnlineRunner(sessionId);
    const entry = conversationRegistry.acquire(sessionId);
    // The landing composer's optimistic bubble (and any status it set) was
    // buffered on the root store because no entry existed yet — hand it over
    // before anything else writes, so the bubble survives the transition.
    adoptPreSessionState(entry);
    // Set boundAgentId/Name from the returned record so the picker doesn't
    // briefly flicker through `null` before bindStream's getSession resolves.
    entry.setState({
      boundAgentId: session.agentId,
      boundAgentName: session.agentName,
      loadingConversation: true,
    });
    useChatStore.setState({ conversationId: sessionId });
    conversationRegistry.setActive(sessionId);
    mirrorActiveEntry();
    opts?.onConversationCreated?.(sessionId);
    queryClient?.invalidateQueries({ queryKey: ["conversations"] });
    await bindStream(sessionId, entrySetter(entry), entryGetter(entry));
  } else {
    const streamCurrent = isConversationStreamCurrent(sessionId);
    const entry = conversationRegistry.acquire(sessionId);
    if (!streamCurrent) {
      // The SSE pump is gone or the last bind failed — most commonly an HTTP
      // intermediary closed the connection on idle, or this conversation was
      // evicted and re-acquired. POSTing without a live pump would queue the
      // message, run the turn, and publish events into an empty subscriber set;
      // the user would never see the response. Rebind first, and fail loud if
      // the rebind itself can't establish the stream.
      //
      // Tear the old pump down first. A snapshot failure leaves
      // `conversationLoadError` set while its pump is STILL OPEN (`bindStream`
      // catches the snapshot error without aborting the controller), so
      // rebinding blind would replace the stored controller and leave two
      // subscribers on one entry — and deltas with no item id, which nothing can
      // dedupe, would then apply twice.
      abortConversationStream(entry);
      entry.setState({ conversationLoadError: null });
      await bindStream(sessionId, entrySetter(entry), entryGetter(entry));
      const loadError = entry.getState().conversationLoadError;
      if (loadError !== null) throw loadError;
    }
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
  // Liveness, not foreground: every live conversation reconciles, so a
  // background one that missed `response.elicitation_resolved` doesn't keep
  // showing a card that was already answered elsewhere. Gating on the visible
  // conversation made a background reconcile fetch the snapshot and discard it.
  if (isConversationDisposed(id)) return;
  const stillPending = new Set(
    (session.pendingElicitations ?? [])
      .map((e) => (typeof e.elicitation_id === "string" ? e.elicitation_id : null))
      .filter((x): x is string => x !== null),
  );
  setterFor(id)((s) => {
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
  | "nativeVendorOwnsModel"
  | "boundAgentId"
  | "boundAgentName"
  | "llmModel"
  | "pendingModelChange"
  | "sessionModelOverride"
  | "sessionHarness"
  | "subAgentName"
  | "costControlModeOverride"
  | "subagentRoutingOverride"
  | "codexPlanMode"
  | "claudePermissionMode"
  | "contextWindow"
  | "gitBranch"
  | "skills"
  | "codexModelOptions"
  | "terminalPending"
  | "sandboxStatus"
  | "mcpStartup"
> {
  const wrapper = session.labels?.["omnigent.wrapper"];
  return {
    isNativeTerminalSession: isNativeWrapper(wrapper),
    // Native wrapper whose model lives in the vendor TUI (no Omnigent picker):
    // qwen/goose/cursor/pi/opencode. nativeModelFamilyForSession is non-null
    // only for claude-/codex-native, which keep the composer model label.
    nativeVendorOwnsModel:
      isNativeWrapper(wrapper) && nativeModelFamilyForSession(session) === null,
    boundAgentId: session.agentId,
    boundAgentName: session.agentName,
    llmModel: session.llmModel ?? null,
    pendingModelChange: null,
    sessionModelOverride: session.modelOverride ?? null,
    sessionHarness: session.harness ?? null,
    subAgentName: session.subAgentName ?? null,
    costControlModeOverride: session.costControlModeOverride ?? null,
    subagentRoutingOverride: session.subagentRoutingOverride ?? null,
    codexPlanMode: codexPlanModeFromSession(session),
    claudePermissionMode: isNativeWrapper(wrapper)
      ? (claudePermissionModeFromSession(session) ?? "")
      : "",
    contextWindow: session.contextWindow ?? null,
    gitBranch: session.gitBranch ?? null,
    skills: session.skills ?? [],
    codexModelOptions: session.codexModelOptions ?? [],
    terminalPending: session.terminalPending ?? false,
    sandboxStatus: session.sandboxStatus ?? null,
    mcpStartup: session.mcpStartup ?? null,
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
  // Apply to the conversation this refresh was for, not whichever is on screen:
  // an agent switch in a backgrounded conversation must still re-derive its
  // binding (most importantly `isNativeTerminalSession`, which gates the
  // optimistic-bubble lifecycle). `setterFor` no-ops once it is evicted.
  setterFor(id)(sessionBindingPatch(session));
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
  racedNativeModelOptions.delete(id);
  const controller = new AbortController();
  // Take an origin-wide stream slot before opening the connection, evicting our
  // own LRU background stream to make room. A fresh tab that finds every slot
  // held by other tabs opens over budget (no slot) and raises the banner.
  await acquireStreamSlot(id);
  if (isConversationDisposed(id)) {
    // Switched away / evicted while awaiting the slot — don't open a dead
    // entry's stream, and hand any slot we took back to the origin.
    releaseStreamSlot(id);
    return;
  }
  set({ abortController: controller });

  // Opening a conversation URL with no session list loaded yet leaves the
  // session→host map empty, so the SSE stream
  // (and every host-scoped request) would open UNKEYED and route to the default
  // replica — NOT the one holding this session's runner tunnel. The live tail
  // then subscribes to the wrong replica's event bus and silently delivers
  // nothing (the server swallows the miss; there's no 503 to trigger the keyless
  // retry). When sharded (a host fetcher is wired), resolve the session's host_id
  // FIRST (one fast metadata GET that populates the map via sessionFromWire) so
  // the stream keys correctly. Best-effort: a failed resolve falls through to the
  // unkeyed open (no worse than pre-fix). Re-apply after every resync, see
  // agentbricks/mas/.claude/skills/sync-omnigents/SKILL.md.
  if (getOmnigentHostConfig().fetcher && getSessionHost(id) === null) {
    try {
      await getSessionSlim(id);
    } catch {
      // Best-effort: a failed resolve (bad id, transient) falls through to the
      // unkeyed open; the snapshot fetch surfaces the real error.
    }
    // Liveness, not the visible id: a background bind must survive a switch away
    // (that is the whole feature). Only a dispose (evicted) bails — and then the
    // slot taken above has to go back to the origin.
    if (isConversationDisposed(id)) {
      releaseStreamSlot(id);
      return;
    }
  }

  // The slot is held for the pump's whole lifetime; released when it exits (a
  // terminal close, an abort from switchTo/dispose, or eviction).
  void startStreamPump(id, controller, set, get).finally(() => releaseStreamSlot(id));

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
  // Bind fetches the whole initial window in one request; nothing loads more
  // until the reader scrolls up (`loadMoreHistory`).
  // `retry: false` because the most common failure here is "invalid conv
  // id in URL" (not transient).
  if (queryClient === null) {
    throw new Error("chatStore.bindStream: queryClient not initialized");
  }
  try {
    // One larger page, so opening a session is a single round trip that then
    // stays still — rather than a small page followed by background growth
    // the reader sees as the transcript shifting seconds after it settled.
    const [session, page] = await Promise.all([
      queryClient.fetchQuery({
        queryKey: ["session", id],
        queryFn: () => getSessionSlim(id, { refreshState: true }),
        staleTime: 0,
        retry: false,
      }),
      fetchSessionItemsPage(id, { limit: INITIAL_WINDOW_ITEMS }),
    ]);
    if (isConversationDisposed(id)) return;
    const items = page.items;

    // Sticky-pref handoff for CLI-created sessions with no override.
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
    // The sticky model is a UI PREFERENCE only: it pre-fills pickers but is
    // never silently PATCHed onto a session and never rendered as the
    // session's model. Display comes from the harness's reported model
    // (`llmModel`); a request exists only when the user explicitly picks.
    // (The old bind-time auto-apply wrote requests the pane was never asked
    // to honor — removed with the reported-model semantics.)
    if (
      !isSubAgentSession &&
      canApplyEffort &&
      session.reasoningEffort == null &&
      stickyEffort != null &&
      !stickyApplyBlocked()
    ) {
      updateSession(id, { reasoningEffort: stickyEffort }).catch((err: unknown) => {
        armStickyApplyBackoff();
        console.warn(`Failed to apply sticky effort=${stickyEffort} to session ${id}:`, err);
      });
    }

    const snapshotBlocks = itemsToBlocks(items);
    // Replay outstanding elicitation prompts from the snapshot.
    // The live SSE stream has no buffer, so a prompt that fired
    // before this chat was opened wouldn't render otherwise.
    const pendingElicitationBlocks = pendingElicitationBlocksFromSnapshot(session);
    const oldestItemId = items[0]?.id ?? null;
    // The sticky pick this bind resolved, applied app-globally after the patch
    // below — but only while this conversation is still on screen. Resolved
    // inside the updater because it depends on the catalog bind race.
    let resolvedStickyModel: string | null = null;
    set((state) => {
      const racedOptions = racedNativeModelOptions.get(id);
      const catalogWonBindRace =
        bindingPatch.codexModelOptions.length === 0 && (racedOptions?.length ?? 0) > 0;
      const effectiveBindingPatch = catalogWonBindRace
        ? { ...bindingPatch, codexModelOptions: racedOptions! }
        : bindingPatch;
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
      // A cold bind prepends the snapshot to whatever the pump already pushed:
      // the entry has no window of its own yet. (The cache-window merge
      // branches that used to live here served the transcript LRU's revisit
      // path, which no longer exists — a revisit finds a live entry and never
      // re-binds.)
      const allBlocks = [
        ...unique,
        ...withoutRebuiltUserInputCards(state.blocks, unique),
        ...uniquePendingElicitations,
      ];
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
      // A cold bind has no live optimistic bubbles of its own — the entry was
      // just created — so every candidate here comes from the server's
      // `pending_inputs`, and deduping against all committed copies is correct.
      // (The old baseline arithmetic existed only because `switchTo` used to
      // destroy state and restore bubbles from a stash, which could collide
      // with an older identical message in history.)
      const dedupePending = hydratePending && candidatePending.length > 0;
      const committedUserTexts = dedupePending ? committedUserTextsOf(allBlocks) : [];
      const countEndsWith = (texts: string[], suffix: string): number =>
        texts.reduce((n, c) => (c.endsWith(suffix) ? n + 1 : n), 0);
      const snapshotPending: PendingUserMessage[] = dedupePending
        ? candidatePending.filter((p) => {
            const text = messageContentText(p.content);
            if (text === "") return true;
            return countEndsWith(committedUserTexts, text) === 0;
          })
        : candidatePending;
      const syntheticError: ErrorBlock | null =
        session.status === "failed" && session.lastTaskError != null && !hasErrorBlock
          ? {
              type: "error",
              ctx: { agent: null, depth: 0, turn: 0, timestamp: 0, responseId: "", itemId: null },
              message: session.lastTaskError.message,
              source: "",
              code: session.lastTaskError.code,
              ...structuredErrorFields(session.lastTaskError),
            }
          : null;
      resolvedStickyModel = stickyModel;
      return {
        ...effectiveBindingPatch,
        blocks: syntheticError !== null ? [...allBlocks, syntheticError] : allBlocks,
        pendingUserMessages: snapshotPending,
        loadingConversation: false,
        hasMoreHistory: page.hasMore,
        oldestItemId,
        // The window cursor was reset: void any in-flight loadMoreHistory.
        historyGeneration: state.historyGeneration + 1,
        // The voided page's stale early-return skips its own flag clear.
        loadingMoreHistory: false,
        sessionStatus: session.status,
        // Mid-turn first open: the snapshot carries the in-flight turn's
        // `activeResponseId`, and the turn-start `running` edge that would
        // have opened the streaming lifecycle is long gone from the SSE
        // stream. Open it here — mirroring `reconnectStatusPatch` — so the
        // live turn's bubble renders streaming (trace expanded, tool
        // spinners live) instead of prematurely settled and folded.
        ...(session.status === "running" &&
        session.activeResponseId != null &&
        state.activeResponse?.responseId !== session.activeResponseId
          ? {
              status: "streaming" as const,
              activeResponse: {
                responseId: session.activeResponseId,
                state: "streaming" as const,
                error: null,
              },
            }
          : {}),
        // Re-show "N background tasks still running" after a reload/navigate-back: the
        // live SSE edge that set this is long gone, so the count rides in on
        // the snapshot (server keeps it sticky past the trailing PTY `idle`).
        backgroundTaskCount: session.backgroundTaskCount ?? 0,
        backgroundTasks: session.backgroundTasks ?? [],
        blockedOn: null,
        // `selectedEffort` / `selectedModel` are app-global sticky picks, not
        // conversation state, so they are applied below — and only while this
        // conversation is still on screen. A cold bind that finishes after the
        // user switched away (or a second concurrent bind) would otherwise
        // overwrite the visible conversation's picker, last-response-wins.
        //
        // This conversation's own effective effort, which is what a warm switch
        // back re-projects (it does not re-bind, so it cannot recompute it).
        sessionReasoningEffort: effectiveEffort,
        // Session truth for the `/model` readout — overrides the snapshot
        // value spread via `...bindingPatch` so the claude-native sticky
        // handoff (fired above, silent) shows immediately.
        sessionModelOverride: session.modelOverride ?? null,
        tokensUsed: session.lastTotalTokens ?? null,
        sessionCostUsd: session.totalCostUsd ?? null,
        sessionUsageByModel: session.usageByModel ?? null,
        todos: (session.todos ?? []) as {
          content: string;
          status: "pending" | "in_progress" | "completed";
          activeForm: string;
        }[],
      };
    });
    // App-global sticky picks: this conversation's snapshot is only allowed to
    // move them while it is the one on screen. A background bind (a switch away
    // mid-fetch, or two cold binds racing) must hydrate its own conversation
    // without touching the visible conversation's picker.
    if (useChatStore.getState().conversationId === id) {
      rootSetState({ selectedEffort: effectiveEffort, selectedModel: resolvedStickyModel });
    }
    racedNativeModelOptions.delete(id);
  } catch (err) {
    if (isConversationDisposed(id)) return;
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
 *   text so far). Appending that replay to a surviving preview would
 *   double the text. A message that committed
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
 *
 * The inverse also matters: when the snapshot shows a turn STILL running and
 * carries its in-flight `activeResponseId`, reopen the streaming
 * `activeResponse`. The SSE stream is snapshot + live tail with no replay, so
 * the turn-start `running` edge that originally opened it is never re-sent —
 * without this, a client connecting mid-turn (reconnect, or first open of an
 * already-running native session) would leave the turn's bubble non-streaming
 * and its tool cards static for the rest of the turn.
 */
function reconnectStatusPatch(session: Session, s: ChatState): Partial<ChatState> {
  const patch: Partial<ChatState> = { sessionStatus: session.status };
  // Recover the background-shell tally across the gap too, so the spinner
  // returns to "N background tasks still running" rather than vanishing on reconnect.
  patch.backgroundTaskCount = session.backgroundTaskCount ?? 0;
  patch.backgroundTasks = session.backgroundTasks ?? [];
  if (session.contextWindow != null) patch.contextWindow = session.contextWindow;
  if (session.lastTotalTokens != null) patch.tokensUsed = session.lastTotalTokens;
  if (session.totalCostUsd != null) patch.sessionCostUsd = session.totalCostUsd;
  if (session.usageByModel != null) patch.sessionUsageByModel = session.usageByModel;
  // `waiting` is a TURN-END snapshot (the turn finished; only background work
  // outlives it), so it settles the local send lifecycle like `idle` — it must
  // NOT reopen a streaming response. The server keeps `active_response_id`
  // populated across `waiting` (it only pops on idle/failed), so grouping
  // `waiting` with `running` below would re-open "streaming" on a reload/
  // reconnect and strand the composer on the "(queued)" placeholder — re-queuing
  // sends, the exact behavior this fix removes. `sessionStatus` stays `waiting`
  // and `backgroundTaskCount` is recovered above, so the spinner survives.
  if (
    (session.status === "idle" || session.status === "failed") &&
    s.activeResponse?.state === "streaming"
  ) {
    patch.activeResponse = {
      ...s.activeResponse,
      state: session.status === "failed" ? "failed" : "completed",
      error: null,
      completedAt: Date.now(),
    };
    patch.status = "idle";
  } else if (session.status === "waiting") {
    // Turn ended, background work remains. Finalize a still-streaming response
    // and free the local send lifecycle so the composer dispatches a new turn.
    if (s.activeResponse?.state === "streaming") {
      patch.activeResponse = {
        ...s.activeResponse,
        state: "completed",
        error: null,
        completedAt: Date.now(),
      };
    }
    patch.status = "idle";
  } else if (
    session.status === "running" &&
    session.activeResponseId != null &&
    s.activeResponse?.responseId !== session.activeResponseId
  ) {
    // Mid-turn (re)connect: reopen the streaming lifecycle from the snapshot.
    // Guarded on a differing responseId so we never downgrade a live
    // activeResponse that already matches (e.g. one cancelled in this tab).
    patch.activeResponse = {
      responseId: session.activeResponseId,
      state: "streaming",
      error: null,
    };
    patch.status = "streaming";
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
 * Drop live question / plan cards that history has already rebuilt.
 *
 * An answered card stays in the block list, and history hydration
 * reconstructs the same card from the persisted tool call once its
 * result lands — so a merge that pulls fresh items in alongside the
 * live tail would show the exchange twice. The two copies carry
 * different elicitation ids (the live one is minted per prompt and
 * never persisted), so they pair on what was asked instead. Only
 * answered cards are dropped: a still-parked prompt is the one the
 * user can act on, and no persisted item can rebuild it.
 *
 * @param liveBlocks - Blocks the live pump produced.
 * @param historyBlocks - Blocks translated from persisted items.
 * @returns `liveBlocks` without the copies history now carries.
 */
function withoutRebuiltUserInputCards(
  liveBlocks: AnyBlock[],
  historyBlocks: AnyBlock[],
): AnyBlock[] {
  const rebuilt = new Set<string>();
  for (const b of historyBlocks) {
    if (b.type !== "elicitation") continue;
    const key = userInputElicitationKey(b);
    if (key !== null) rebuilt.add(key);
  }
  if (rebuilt.size === 0) return liveBlocks;
  return liveBlocks.filter((b) => {
    if (b.type !== "elicitation" || b.status !== "responded") return true;
    const key = userInputElicitationKey(b);
    return key === null || !rebuilt.has(key);
  });
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
 * backfill cap: replace the history window wholesale from one fresh window
 * fetch, exactly as a cold bind does — same size, same single round trip.
 * The reader did not ask for this either (it fires off a dropped stream), so
 * paging it in over several requests would shift the transcript under them
 * for the same reason opening a session used to. Pre-gap blocks are
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
    fresh = await fetchSessionItemsPage(id, { limit: INITIAL_WINDOW_ITEMS });
  } catch {
    return;
  }
  if (isConversationDisposed(id) || get().historyGeneration !== generation) return;
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
    const windowBlocks = freshBlocks.filter((b) => !b.ctx.itemId || !tailIds.has(b.ctx.itemId));
    const merged = [...windowBlocks, ...withoutRebuiltUserInputCards(tail, windowBlocks)];
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
  const stale = (): boolean => isConversationDisposed(id) || get().historyGeneration !== generation;
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
  // Each page starts before the cursor returned by the prior page.
  /* oxlint-disable no-await-in-loop */
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
  /* oxlint-enable no-await-in-loop */
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
    // `session.input.consumed` is not replayed, so recovered user blocks are
    // the durable equivalent of its FIFO acknowledgement.
    const recoveredUserInputs = unseen.filter(
      (b) => b.type === "user_message" && !isSystemUserContent(b.content),
    ).length;
    if (recoveredUserInputs > 0) {
      patch.pendingUserMessages = s.pendingUserMessages.slice(recoveredUserInputs);
    }
    let nextBlocks = s.blocks;
    if (unseen.length > 0) {
      // Splice the gap's committed items ahead of the active turn's
      // replayed in-flight region (its itemId-less blocks, rebuilt by the
      // pump at the tail). With no replay region yet, anchor AFTER the
      // rid's last block: the rid's gap items are newer than its pre-gap
      // committed blocks, so before-its-first would invert the bubble.
      // No rid blocks at all: append; the later replay lands after.
      const rid = s.activeResponse?.state === "streaming" ? s.activeResponse.responseId : null;
      // A card answered before the gap whose call the gap persisted comes
      // back rebuilt in `unseen` — drop the live copy before anchoring.
      const kept = withoutRebuiltUserInputCards(s.blocks, unseen);
      let at = -1;
      if (rid) {
        at = kept.findIndex((b) => b.ctx.responseId === rid && !b.ctx.itemId);
        if (at === -1) {
          const lastRid = kept.findLastIndex((b) => b.ctx.responseId === rid);
          if (lastRid !== -1) at = lastRid + 1;
        }
      }
      nextBlocks =
        at >= 0 ? [...kept.slice(0, at), ...unseen, ...kept.slice(at)] : [...kept, ...unseen];
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
// One entry per live stream: an idle flip must recycle EVERY open stream,
// since each carries its own `idle` uplink. A single controller would leave
// all but one stream reporting a stale flag once streams outlive a switch.
const presenceAttemptControllers = new Set<AbortController>();
const presenceIdle = createPresenceIdleTracker({
  onFlip: () => {
    // Copy first: aborting settles each attempt's `finally`, which mutates
    // this set while we iterate it.
    for (const controller of [...presenceAttemptControllers]) controller.abort();
  },
});

// ── Stream liveness ─────────────────────────────────────────────────
// When bytes last arrived on each live stream attempt (heartbeats
// included), stamped from the moment the attempt starts. Lets the fast
// paths below tell a stream that is merely quiet from one that is
// probably dead — tracked per attempt, since background conversations
// hold streams of their own.
const streamAttemptActivity = new Map<AbortController, number>();

// Two missed 15 s server heartbeats plus slack. Deliberately shorter than
// the stall guard's own window (`SSE_STALL_TIMEOUT_MS`): the guard is the
// ceiling; these event-driven checks make the common cases immediate.
export const SSE_STALE_RECYCLE_MS = 35_000;

/**
 * Recycle every live stream attempt that looks dead — no bytes for
 * `SSE_STALE_RECYCLE_MS` while its connection is supposedly up.
 *
 * Fired on the two moments a half-open socket is most likely to be
 * discovered: the tab becoming visible again (wake from sleep) and the
 * browser regaining network. Aborting only the per-attempt controller
 * funnels each pump into its normal reconnect + snapshot reconcile
 * (background pumps add their own jitter); a healthy stream (fresh
 * bytes) is left untouched, so alt-tabbing never churns connections.
 */
function recycleStreamIfStale(): void {
  const now = Date.now();
  // Copy first: aborting settles each attempt's `finally`, which deletes
  // from this map while we iterate it.
  for (const [attempt, lastActivityAt] of [...streamAttemptActivity]) {
    if (now - lastActivityAt > SSE_STALE_RECYCLE_MS) attempt.abort();
  }
}

if (typeof document !== "undefined") {
  document.addEventListener("visibilitychange", () => {
    presenceIdle.handleVisibilityChange(document.hidden);
    if (!document.hidden) recycleStreamIfStale();
  });
}
if (typeof window !== "undefined") {
  window.addEventListener("online", () => recycleStreamIfStale());
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
  // Consecutive 404s only — reset on any non-404 outcome (success or a
  // different-status failure), so a 404 has to persist across attempts to
  // count toward the cap below.
  let consecutive404s = 0;
  // True once we've had at least one SUCCESSFUL open. Drives reconnect-only
  // behavior (drop in-flight + reconcile), which must NOT run on the first
  // established stream — failed opens leave it false so a recovered first
  // connect is still treated as initial, not a reconnect.
  let hasConnected = false;
  // A reconnect loop is inherently sequential — open → pump → reconnect —
  // so its awaits cannot be parallelized; no-await-in-loop doesn't apply.
  /* eslint-disable no-await-in-loop */
  try {
    while (!controller.signal.aborted && !isConversationDisposed(id)) {
      // Back off only between consecutive failed opens. A drop after a
      // healthy connection (the benign ~5-min ingress recycle) leaves
      // failedOpens at 0, so it reconnects instantly with no delay.
      if (failedOpens > 0) {
        await abortableDelay(nextReconnectDelay(failedOpens), controller.signal);
        if (controller.signal.aborted || isConversationDisposed(id)) break;
      } else if (hasConnected && conversationRegistry.getActive()?.id !== id) {
        // Stagger a BACKGROUND conversation's reconnect. The ingress caps every
        // stream at ~5 minutes, and streams opened together recycle together, so
        // without this a tab holding N conversations fires N reconnects — each
        // with a snapshot + items fetch — in one burst. The conversation on
        // screen is never delayed.
        await abortableDelay(backgroundReconnectJitter(), controller.signal);
        if (controller.signal.aborted || isConversationDisposed(id)) break;
      }

      // Per-attempt controller: a presence idle flip recycles just this
      // connection (the `idle` query param is the entire presence uplink,
      // so the flip must arrive as a reconnect). Outer aborts (switchTo /
      // unmount) forward in so teardown still cancels the live fetch.
      const attempt = new AbortController();
      const onOuterAbort = () => attempt.abort();
      controller.signal.addEventListener("abort", onOuterAbort);
      presenceAttemptControllers.add(attempt);
      // Stamped from attempt start so the wake fast-path can also recycle
      // an open that has hung past the stale window, not just a dead body.
      streamAttemptActivity.set(attempt, Date.now());
      try {
        const idle = presenceIdle.idleNow();
        let streamRes: Response;
        try {
          streamRes = await openSessionStream(id, attempt.signal, { idle });
        } catch (err) {
          if (err instanceof Error && err.name === "AbortError") {
            if (controller.signal.aborted || isConversationDisposed(id)) break;
            // Only the attempt was aborted (presence flip mid-open) —
            // reopen immediately with the recomputed idle flag.
            continue;
          }
          if (isConversationDisposed(id)) break;
          console.warn(`Session ${id}: stream connect failed, will retry`, err);
          failedOpens += 1;
          continue;
        }

        if (controller.signal.aborted || isConversationDisposed(id)) break;
        if (!streamRes.ok || !streamRes.body) {
          // Release the unconsumed error-response body so the underlying fetch
          // connection is freed promptly rather than lingering across retries.
          void streamRes.body?.cancel().catch(() => {});
          // 401/403 won't fix themselves by retrying — give up and settle the
          // local send lifecycle so the user isn't left on a silent spinner.
          // `sessionStatus` is NOT touched: losing our stream says nothing
          // about what the agent is doing (it may well still be mid-turn), and
          // only the server may declare a session failed. The dropped stream
          // surfaces as offline liveness via ConnectionIndicator.
          if (streamRes.status === 401 || streamRes.status === 403) {
            console.warn(`Session ${id}: stream unavailable (${streamRes.status}), giving up`);
            finalizeActive(set, "failed", `stream unavailable (${streamRes.status})`, null);
            set({ status: "idle" });
            break;
          }
          // A reverse proxy routinely serves 404 for the stream route while
          // the backend container restarts, so treat it like a transient
          // failure up to a cap — only a 404 that outlives that window (a
          // truly deleted/invalid conversation) gives up.
          if (streamRes.status === 404) {
            consecutive404s += 1;
            if (consecutive404s > MAX_TRANSIENT_404_RETRIES) {
              console.warn(
                `Session ${id}: stream unavailable (404) after ${consecutive404s} attempts, giving up`,
              );
              // Local lifecycle only — see the 401/403 branch above for why
              // `sessionStatus` is left to the server.
              finalizeActive(set, "failed", "stream unavailable (404)", null);
              set({ status: "idle" });
              break;
            }
            console.warn(
              `Session ${id}: stream open failed (404, attempt ${consecutive404s}/${MAX_TRANSIENT_404_RETRIES}), will retry`,
            );
            failedOpens += 1;
            continue;
          }
          console.warn(`Session ${id}: stream open failed (${streamRes.status}), will retry`);
          failedOpens += 1;
          continue;
        }
        // An auth layer in front of the server (e.g. an expired app-ingress
        // session) can answer with a redirect the fetch follows to a 200
        // text/html login page. Pumping that body would end instantly
        // without `[DONE]` and reconnect with no backoff — a hot loop that
        // leaves the transcript silently frozen. Treat a non-SSE content
        // type as a failed open so it backs off like any other bad answer.
        const contentType = streamRes.headers.get("content-type") ?? "";
        if (!contentType.toLowerCase().includes("text/event-stream")) {
          void streamRes.body.cancel().catch(() => {});
          console.warn(`Session ${id}: stream open returned '${contentType}', will retry`);
          failedOpens += 1;
          continue;
        }

        const reconnecting = hasConnected;
        hasConnected = true;
        failedOpens = 0;
        consecutive404s = 0;
        presenceIdle.noteReported(idle);
        streamAttemptActivity.set(attempt, Date.now());
        if (reconnecting) {
          dropEphemeralInFlightBlocks(id, set);
        } else {
          // Fresh connection (not a reconnect) — clear any stale SSE log from
          // a previous stream bind so the debug panel starts clean.
          clearSseLog(id);
        }
        // Guard the byte stream with a silence watchdog: the server
        // heartbeats every 15 s, so a longer gap means a half-open socket
        // (laptop sleep, network path change, proxy reap). The guard ends
        // the stream like a transport drop, and this loop's reconnect +
        // reconcile resupplies whatever committed during the dead window —
        // without it the pump blocks in read() forever and the transcript
        // silently freezes until a page reload.
        const guardedBody = withStallGuard(streamRes.body, {
          onActivity: () => {
            streamAttemptActivity.set(attempt, Date.now());
          },
          onStall: () =>
            console.warn(
              `Session ${id}: no stream bytes in ${SSE_STALL_TIMEOUT_MS} ms; reconnecting`,
            ),
        });
        // Start the pump, then reconcile the snapshot concurrently (race-safe
        // via itemId dedup) — mirrors bindStream's stream-then-snapshot order.
        const pumpPromise = pumpStreamEvents(id, guardedBody, controller, set, get);
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
        presenceAttemptControllers.delete(attempt);
        streamAttemptActivity.delete(attempt);
      }
    }
  } finally {
    if (get().abortController === controller) {
      set({ abortController: null });
    }
  }
  /* eslint-enable no-await-in-loop */
}

// Spread background reconnects over a few seconds so N conversations recycling
// at the same ingress deadline don't fire N snapshot fetches at once. Small
// enough that a backgrounded conversation is still current well before the user
// could switch to it.
const BACKGROUND_RECONNECT_JITTER_MAX_MS = 3_000;

function backgroundReconnectJitter(): number {
  return Math.random() * BACKGROUND_RECONNECT_JITTER_MAX_MS;
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

/** Whether a block is a provisional live-streaming text preview. */
function isLiveProvisionalBlock(b: AnyBlock): boolean {
  return b.ctx.itemId?.startsWith(LIVE_ITEM_PREFIX) ?? false;
}

/**
 * Build a provisional in-flight assistant-text block for live streaming.
 *
 * Shaped like a finalized `text_done` so the existing renderer draws it
 * as assistant text, and keyed with a synthetic `live:<messageId>` id
 * so it can be removed when the authoritative item lands.
 *
 * `responseId` is the LIVE TURN's id whenever one is streaming, so the
 * preview groups into that turn's bubble (`walkBubbles` groups by
 * response id). Giving it a synthetic id instead split one native turn
 * into several fragment bubbles while streaming that merged back into
 * one on reload — so a turn rendered differently live vs. reloaded, no
 * fragment had the process-plus-answer shape the "Worked for" fold
 * needs, and shifting fragment boundaries made the fold flicker. Falls
 * back to the synthetic id when no turn is streaming (a preview that
 * arrives before the turn's id is known must not join the PREVIOUS
 * turn's bubble).
 *
 * :param itemId: the provisional id, e.g. ``"live:2ca51d97-..."``.
 * :param text: the text accumulated so far, e.g. ``"Hello"``.
 * :param responseId: the live turn's id, or `itemId` when none.
 * :returns: a `TextDone` block ready to push into `blocks`.
 */
function makeLiveTextBlock(itemId: string, text: string, responseId: string): TextDone {
  return {
    type: "text_done",
    // ``timestamp`` matches the reducer's monotonic source (not wall
    // clock); it is an ordering hint, not a displayed date.
    ctx: {
      agent: null,
      depth: 0,
      turn: 0,
      timestamp: performance.now() / 1000,
      responseId,
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
 * position the first chunk arrived. The authoritative `text_done` removes
 * this provisional block before following the normal committed-item path.
 *
 * The server reconciles and deduplicates chunks, so each received delta is
 * appended directly.
 *
 * :param set: store setter.
 * :param messageId: vendor's stable per-message id.
 * :param delta: incremental text for this chunk, e.g. ``"Hello "``.
 * :returns: nothing; mutates `blocks` in the store.
 */
function applyLiveDelta(set: Setter, messageId: string, delta: string): void {
  const itemId = LIVE_ITEM_PREFIX + messageId;
  set((s) => {
    const at = s.blocks.findIndex((b) => b.ctx.itemId === itemId);
    if (at === -1) {
      const live = s.activeResponse;
      const responseId = live?.state === "streaming" ? live.responseId : itemId;
      return { blocks: [...s.blocks, makeLiveTextBlock(itemId, delta, responseId)] };
    }
    const existing = s.blocks[at]!;
    if (existing.type !== "text_done") return {};
    const fullText = existing.fullText + delta;
    const next = s.blocks.slice();
    next[at] = { ...existing, fullText, hasCodeBlocks: fullText.includes("```") };
    return { blocks: next };
  });
}

/** Append live output to the matching in-progress tool execution. */
function applyLiveToolOutputDelta(set: Setter, callId: string, delta: string): void {
  set((s) => {
    const at = s.blocks.findIndex(
      (b): b is ToolGroup =>
        b.type === "tool_group" && b.executions.some((execution) => execution.callId === callId),
    );
    if (at === -1) return {};
    const group = s.blocks[at] as ToolGroup;
    const executions = group.executions.map((execution) =>
      execution.callId === callId
        ? { ...execution, output: (execution.output ?? "") + delta }
        : execution,
    );
    const next = s.blocks.slice();
    next[at] = { ...group, executions };
    return { blocks: next };
  });
}

/**
 * Restore the "mid-turn" signal after a stray `completed` edge.
 *
 * A live delta proves the turn is still going even though something flipped
 * `activeResponse` to `completed` (a stray idle edge, an out-of-order status).
 * Reopen it to streaming and restore `sessionStatus: "running"` so send-gating
 * queues instead of firing into the live turn and the Working indicator returns
 * before the next `running` edge. Local `status` is left alone — it means "this
 * client's send is in flight", which is false for cross-client / TUI turns.
 */
export function reviveStrayCompletedResponse(set: Setter): void {
  set((s) => {
    if (s.activeResponse?.state !== "completed") return {};
    if (isStaleCompletedResponse(s)) return {};
    return {
      activeResponse: { ...s.activeResponse, state: "streaming" },
      sessionStatus: "running",
    };
  });
}

/**
 * Wrap a parsed event stream, diverting terminal-observed live deltas.
 *
 * A `text_delta` carrying a `messageId` is native live streaming:
 * it is folded into its provisional preview block in `blocks` (see
 * `applyLiveDelta`) and NOT yielded downstream, because the `BlockStream`
 * reducer's response-scoped text path would otherwise emit a stray bubble
 * (these deltas carry no response id and their authoritative text arrives
 * as a separate committed item). Every other event passes through
 * untouched.
 *
 * :param events: upstream parsed events (already session-tapped).
 * :param id: the conversation this pump is bound to; a late delta from a
 *     switched-away stream is dropped rather than mutating state.
 * :param ignored: message ids suppressed because their scheduled-wake
 *     deltas arrived before the new turn was named.
 * :param set: store setter.
 * :param get: store getter.
 * :returns: events with native live deltas removed.
 */
async function* tapLiveDeltas(
  events: AsyncIterable<StreamEvent>,
  id: string,
  ignored: Set<string>,
  set: Setter,
  get: Getter,
): AsyncIterable<StreamEvent> {
  for await (const ev of events) {
    if (ev.type === "text_delta" && ev.messageId !== undefined) {
      if (!isConversationDisposed(id) && !ignored.has(ev.messageId)) {
        // A scheduled wake streams its first deltas ahead of the batch
        // that names the new turn. They must not preview into the
        // PREVIOUS turn's bubble (anonymous blocks glue to the trailing
        // group — killing its fold and inflating its worked-for span):
        // ignore the rest of the message so its text renders only via the
        // authoritative item, which lands in the new turn's bubble.
        if (isStaleCompletedResponse(get())) {
          ignored.add(ev.messageId);
          continue;
        }
        applyLiveDelta(set, ev.messageId, ev.delta);
      }
      continue;
    }
    if (ev.type === "tool_output_delta") {
      if (!isConversationDisposed(id) && !isStaleCompletedResponse(get())) {
        reviveStrayCompletedResponse(set);
        applyLiveToolOutputDelta(set, ev.callId, ev.delta);
      }
      continue;
    }
    if (
      (ev.type === "text_delta" || ev.type === "reasoning_delta") &&
      !isConversationDisposed(id)
    ) {
      reviveStrayCompletedResponse(set);
    }
    yield ev;
  }
}

/**
 * Attribute the trailing run of turn-id-less blocks to a just-started turn.
 *
 * A native harness sends no `response.created`, and codex opens its
 * reasoning block a couple of seconds BEFORE the `running` status edge
 * that carries the turn id — so those blocks are stamped with an empty
 * id. `walkBubbles` groups by response id, so they render as a bubble of
 * their own next to the turn's committed items instead of inside it.
 * Only the trailing empty-id run is adopted, so nothing older moves.
 *
 * @returns the rewritten blocks, or `null` when nothing needed adopting.
 */
export function adoptTrailingUnattributedBlocks(
  blocks: AnyBlock[],
  responseId: string,
): AnyBlock[] | null {
  let start = blocks.length;
  while (start > 0 && blocks[start - 1]!.ctx.responseId === "") start -= 1;
  if (start === blocks.length) return null;
  const next = blocks.slice();
  for (let i = start; i < next.length; i += 1) {
    const b = next[i]!;
    next[i] = { ...b, ctx: { ...b.ctx, responseId } };
  }
  return next;
}

// How long after a terminal edge a delta still belongs to the finished
// turn. A scheduled wake (cron / wakeup fires at 60s minimum) streams
// its FIRST deltas ahead of the transcript batch that names the new
// turn; attributing those to the previous turn popped its "Worked for"
// fold open at the start of every /loop iteration.
const REVIVE_WINDOW_MS = 15_000;

/**
 * Whether the finished turn is too old for a delta to plausibly belong
 * to it — such deltas open the NEXT turn (a scheduled wake).
 */
export function isStaleCompletedResponse(s: { activeResponse: ActiveResponse | null }): boolean {
  return (
    s.activeResponse?.state === "completed" &&
    s.activeResponse.completedAt !== undefined &&
    Date.now() - s.activeResponse.completedAt > REVIVE_WINDOW_MS
  );
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

// Product-analytics interaction tracking (agent runs, tool calls). START stamps
// a timestamp keyed by the run/tool id; COMPLETE reads-and-deletes it, so each
// interaction emits start/complete exactly once (guarding SSE re-delivery on
// reconnect) and carries a duration. Only the LIVE pump below writes these —
// history hydration goes through `reduceSync`, which never runs this loop, so
// reopening an old conversation never re-emits.
const runStartTimes = new Map<string, number>();
const toolStartTimes = new Map<string, number>();

function mapRunStatus(state: string): OmnigentInteractionStatus {
  switch (state) {
    case "completed":
      return "success";
    case "failed":
      return "failure";
    // "incomplete"/"cancelled" are both a stopped turn from the user's view.
    case "incomplete":
    case "cancelled":
      return "cancelled";
    default:
      return "failure";
  }
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
  // A scheduled wake can stream before its new turn id arrives. Ignore the
  // rest of that message so it cannot attach to the completed prior turn.
  const ignoredWakeMessages = new Set<string>();
  const events = tapLiveDeltas(tapSessionEvents(rawEvents, id), id, ignoredWakeMessages, set, get);

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
    if (isConversationDisposed(id)) {
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
      if (isConversationDisposed(id)) return "switched";

      if (block.type === "response_start") {
        // New response: force-flush whatever is buffered, then land the
        // marker + lifecycle in one commit. Reset the first-paint latch.
        paintedFirstContent = false;
        flush(block, {
          activeResponse: { responseId: block.responseId, state: "streaming", error: null },
          status: "streaming",
        });
        if (block.responseId && !runStartTimes.has(block.responseId)) {
          runStartTimes.set(block.responseId, Date.now());
          emitInteractionPhase({
            interactionId: block.responseId,
            interactionKind: "agent_run",
            phase: "start",
          });
        }
        continue;
      }

      // Native preview cleanup must run before the generic dedup below:
      // when a snapshot merge (reconnect/rebind) inserted this
      // authoritative item while its `live:*` preview was still on
      // screen, the dedup alone would drop the event and strand the
      // preview as a trailing duplicate of the assistant text. Remove
      // the oldest preview, then fall through so the dedup skips the
      // event as before.
      if (
        block.type === "text_done" &&
        block.ctx.itemId &&
        get().isNativeTerminalSession &&
        (seenItemIds.has(block.ctx.itemId) ||
          get().blocks.some((b) => b.ctx.itemId === block.ctx.itemId))
      ) {
        const provIdx = get().blocks.findIndex(isLiveProvisionalBlock);
        if (provIdx !== -1) {
          flush();
          set((s) => {
            const at = s.blocks.findIndex(isLiveProvisionalBlock);
            if (at === -1) return {};
            const next = s.blocks.slice();
            next.splice(at, 1);
            return { blocks: next };
          });
        }
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
          // The done item has no message id. Native messages are sequential,
          // so remove the oldest preview and let the committed item follow
          // the normal reducer path.
          flush();
          set((s) => {
            const at = s.blocks.findIndex(isLiveProvisionalBlock);
            if (at === -1) return {};
            const next = s.blocks.slice();
            next.splice(at, 1);
            return { blocks: next };
          });
          paintedFirstContent = false;
        }
      }

      if (block.type === "response_end") {
        // Force-flush buffer + marker before the terminal side effects so
        // the bubble's final content commits with its lifecycle transition.
        flush(block);
        // Ignore a terminal for a response that is NOT the currently-active
        // one. A native-terminal harness (e.g. hermes-native) can emit an
        // empty runner wrapper response that completes AFTER the forwarder's
        // per-turn id has already taken over `activeResponse`; applying this
        // stale terminal would downgrade the live turn to "completed" (its
        // tool cards stop streaming), flip the session to idle, and prune the
        // in-flight preview — the first-turn "no spinner" bug. On a matching
        // (or absent) active response this is the normal terminal path.
        const active = get().activeResponse;
        const endedId = block.response?.id ?? block.ctx?.responseId ?? "";
        if (active !== null && active.responseId !== endedId) {
          continue;
        }
        // If the active response was already marked cancelled by an
        // earlier `session.interrupted`, keep that. Session events
        // are the authoritative source for user-initiated terminals.
        if (active?.state !== "cancelled") {
          const errorMsg = block.response?.error?.message ?? null;
          finalizeActive(set, block.status as ActiveResponse["state"], errorMsg);
        }
        const runStart = runStartTimes.get(endedId);
        if (endedId && runStart !== undefined) {
          runStartTimes.delete(endedId);
          emitInteractionPhase({
            interactionId: endedId,
            interactionKind: "agent_run",
            phase: "complete",
            status:
              active?.state === "cancelled" ? "cancelled" : mapRunStatus(String(block.status)),
            durationMs: Date.now() - runStart,
          });
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
          queryClient?.invalidateQueries({ queryKey: sessionItemsQueryKey(convId) });
          // No terminals invalidation: the list is SSE-sourced (see
          // useTerminals). Its query has only an empty seed queryFn, so
          // invalidating would refetch [] and wipe the live list. The
          // session.resource.{created,deleted} deltas already keep it
          // fresh during the turn, and snapshot-on-connect re-seeds it
          // on reconnect.
        }
        continue;
      }

      // Tool-call analytics (live only). No reliable success/failure signal on
      // the result block — tool errors surface as separate error events — so we
      // report invocation + duration + tool name, not an outcome status.
      if (block.type === "tool_group") {
        for (const ex of block.executions) {
          if (ex.callId && !toolStartTimes.has(ex.callId)) {
            toolStartTimes.set(ex.callId, Date.now());
            emitInteractionPhase({
              interactionId: ex.callId,
              interactionKind: "tool_call",
              phase: "start",
              name: ex.name,
            });
          }
        }
      } else if (block.type === "tool_result") {
        const toolStart = toolStartTimes.get(block.callId);
        if (block.callId && toolStart !== undefined) {
          toolStartTimes.delete(block.callId);
          emitInteractionPhase({
            interactionId: block.callId,
            interactionKind: "tool_call",
            phase: "complete",
            name: block.name,
            durationMs: Date.now() - toolStart,
          });
        }
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
    if (isConversationDisposed(id)) return "switched";
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
  createdAtS?: number,
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
      // Client clock — see blockStream.ctx; keeps server-stamped
      // `createdAtS` comparisons single-clock. A promoted optimistic
      // bubble keeps its send-time stamp rather than re-stamping now.
      ...(createdAtS !== undefined ? { clientCreatedAtS: createdAtS } : {}),
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
  /** The server has signalled that its model-options cache is populated. */
  modelOptionsResolved?: boolean;
}

/**
 * Refetch runner-backed session state and apply it to the store.
 *
 * Skills and native model options are runner-owned. When a session
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
  // Liveness, not foreground: `session_skills` / `session_model_options` are
  // one-shot nudges with no replay, and a live background conversation is never
  // re-bound on return — dropping the nudge here would leave its slash menu and
  // model catalog empty for as long as the entry stays live.
  if (isConversationDisposed(conversationId)) return;
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
    if (options.modelOptionsResolved === true && (session.codexModelOptions ?? []).length === 0) {
      // The event can race the bind snapshot's in-flight query. Once that
      // request settles, issue a second read instead of accepting its stale [].
      session = await getSessionSlim(conversationId);
      queryClient?.setQueryData(["session", conversationId], session);
    }
  } catch {
    // The runner may have dropped again before the fetch landed. Keep
    // the existing state rather than wiping it on a transient error.
    return;
  }
  // The conversation may have been backgrounded (or evicted) while the request
  // was in flight. Runner-backed state is conversation-scoped, so apply it to
  // the conversation it was fetched for rather than dropping it — a background
  // session's resolved skills / model catalog must be there when the user
  // returns. `setterForState` / `setterFor` no-op once it has been evicted.
  const currentState = setterForState(conversationId);
  if (currentState === null) return;
  if (options.modelOptionsResolved === true && (session.codexModelOptions ?? []).length > 0) {
    racedNativeModelOptions.set(conversationId, session.codexModelOptions ?? []);
  }
  // No sticky-model graft here: the sticky pick is a pure preference under
  // the reported-model semantics, so a delayed catalog only hydrates state.
  const statePatch: Partial<ConversationState> =
    options.applyBindingPatch === true
      ? sessionBindingPatch(session)
      : {
          skills: session.skills ?? [],
          codexModelOptions: session.codexModelOptions ?? [],
        };
  setterFor(conversationId)(statePatch);
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
 * the snapshot replay in `bindStream`.
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
 * Apply store side effects for a `session.*` SSE event.
 *
 * Exported for direct unit testing — production code reaches this
 * via the `tapSessionEvents` generator wrapping the parsed SSE
 * stream inside `pumpStreamEvents`. The reducer (`BlockStream`)
 * deliberately ignores these events; session-scoped state lives on
 * the store, not on the reducer's internal state machine.
 *
 * :param event: the parsed stream event.
 * :param streamConversationId: the conversation whose stream delivered this
 *     event — the conversation these writes land on, foreground or background.
 *     See {@link applyToConversation}. Omit only in tests that mean "as if
 *     delivered by the active conversation's stream".
 *
 * No-op for events outside the `session.*` family.
 */
export function handleSessionEvent(event: StreamEvent, streamConversationId?: string): void {
  // Routing is by DELIVERING STREAM, not by event payload: several events
  // (`session.input.consumed`, `session.interrupted`, `session.resource.created`)
  // carry no conversation id at all, so their contents can't say where they
  // belong. Default to the active conversation so a caller that omits the id
  // keeps today's behaviour.
  const sourceConversationId = streamConversationId ?? useChatStore.getState().conversationId;

  /**
   * Write conversation-scoped state onto the conversation whose stream
   * delivered this event — which may be a BACKGROUND one.
   *
   * Every branch below that touches `ConversationState` must go through this
   * rather than `useChatStore.setState` directly. That setter writes the root
   * store, which is only a projection of the conversation on screen: a
   * background stream's writes would be dropped there (its events would vanish,
   * leaving e.g. an optimistic user bubble that never commits), and an
   * unguarded write would paint its status onto the visible conversation.
   * Routing to the entry does both jobs at once — the registry mirrors the
   * active entry back onto the root, so the visible conversation still updates.
   *
   * No-op when the conversation is no longer live (evicted / released), so
   * a late event can't resurrect state for it.
   */
  const applyToConversation = (
    patch: Partial<ConversationState> | ((s: ChatState) => Partial<ConversationState>),
  ): void => {
    setterFor(sourceConversationId)(patch);
  };

  /**
   * Same as {@link applyToConversation}, for events that name their own target.
   *
   * Most `session.*` events carry a `conversationId`. That payload id is
   * authoritative — a session's stream can carry frames about a DIFFERENT
   * conversation (a sub-agent's, or a rotated-away session's) — so it has to
   * agree with the delivering stream before the write applies.
   */
  const applyToNamedConversation = (
    namedConversationId: string,
    patch: Partial<ConversationState> | ((s: ChatState) => Partial<ConversationState>),
  ): void => {
    if (namedConversationId !== sourceConversationId) return;
    applyToConversation(patch);
  };

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
          applyToConversation({ tokensUsed: ringTokens });
        }
      }
      return;
    case "session_todos":
      // Replace the todo list entirely — each event carries the full
      // current list, not a diff.
      applyToConversation({ todos: event.todos });
      return;
    case "session_terminal_pending":
      // Toggle the Terminal-pill spinner. The runner sets pending=true
      // before auto-creating the terminal and clears it once the
      // terminal lands or auto-create fails.
      applyToConversation({ terminalPending: event.pending });
      return;
    case "session_sandbox_status":
      // Advance the managed-sandbox provisioning indicator. `ready`
      // clears it — from then on the session looks like any
      // host-bound session; `failed` retains the reason so the page
      // explains why the sandbox never came up.
      applyToConversation({
        sandboxStatus: event.stage === "ready" ? null : { stage: event.stage, error: event.error },
      });
      return;
    case "session_mcp_startup": {
      // Mirror the harness's per-MCP-server startup map. Cleared once
      // every server settles `ready` (the band disappears); failures and
      // cancellations are retained so the page can say which servers
      // never came up.
      const records = Object.values(event.servers);
      const allReady = records.length === 0 || records.every((r) => r.status === "ready");
      applyToConversation({ mcpStartup: allReady ? null : event.servers });
      return;
    }
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
        applyToConversation(patch);
      }
      return;
    }
    case "session_model":
      // The harness's model report — the launch's own model, or a switch
      // made inside the pane. The value is VERBATIM (the harness's own
      // spelling); it lands on `llmModel`, the reported-model slot every
      // display surface renders from. The request (`sessionModelOverride`)
      // and the cross-session sticky are deliberately untouched: reports
      // and requests are separate roles. The report also settles any
      // pending switch: whatever the harness says it runs now IS the
      // outcome of the ask.
      applyToNamedConversation(event.conversationId, {
        llmModel: event.model,
        pendingModelChange: null,
      });
      return;
    case "error":
      // A `model_change_not_applied` error is the loud outcome of a model
      // ask the pane never took: settle the pending indicator (the chip
      // already shows the true model). The error block itself renders
      // through the BlockStream reducer as usual.
      if (event.error.code === "model_change_not_applied") {
        applyToConversation((s) =>
          s.pendingModelChange !== null ? { pendingModelChange: null } : {},
        );
      }
      return;
    case "session_title":
      // A `/rename` typed inside a native terminal. The server already
      // persisted `title`, so a reload restores it; patch the caches the
      // sidebar and session snapshot render from so the new name shows
      // without waiting for the next list reconcile. Writes are keyed by
      // the event's own conversation id, so no open-session guard is
      // needed (this stream only carries the open session anyway).
      // Sessions renamed while NOT open converge via the
      // `WS /v1/sessions/updates` diff instead.
      if (queryClient !== null) {
        overlayTitleIntoCaches(queryClient, event.conversationId, event.title);
      }
      return;
    case "session_reasoning_effort":
      // A thinking-level switch made inside a native terminal. The session's own
      // effective effort always lands, background included — a live conversation
      // is never re-bound on return, so dropping it here would leave the picker
      // wrong until a refresh.
      applyToNamedConversation(event.conversationId, {
        sessionReasoningEffort: event.reasoningEffort,
      });
      // `selectedEffort` is the app-global sticky pick, so only adopt a value
      // reported by the conversation the user is actually looking at.
      if (
        event.conversationId === sourceConversationId &&
        useChatStore.getState().conversationId === event.conversationId
      ) {
        useChatStore.setState({ selectedEffort: event.reasoningEffort });
      }
      return;
    case "session_permission_mode":
      // Pane-driven (in-TUI shift+tab) or UI-driven; either way the server
      // has confirmed this is the session's live mode.
      applyToNamedConversation(event.conversationId, {
        claudePermissionMode: event.permissionMode,
      });
      return;
    case "session_collaboration_mode":
      // A Codex /plan switch made in either the web UI or native TUI.
      // Guard by conversation id so a late frame from an aborted stream
      // cannot paint Plan mode onto the newly-opened conversation.
      applyToNamedConversation(event.conversationId, { codexPlanMode: event.mode === "plan" });
      return;
    case "session_presence":
      // Full-state replacement — every presence event carries the
      // complete viewer list, so there is no join/leave ordering to
      // get wrong. Guarded by conversation id so a late frame from a
      // switched-away stream can't paint another session's viewers.
      applyToNamedConversation(event.conversationId, { viewers: event.viewers });
      return;
    case "session_agent_changed":
      // The session's bound agent was switched in place (switch-agent
      // route). Apply the binding the event itself carries immediately,
      // then re-derive the label-dependent state (most importantly
      // isNativeTerminalSession, which gates the optimistic-bubble
      // lifecycle) from a fresh snapshot — the event is the only signal
      // an in-place switch produces; the URL doesn't change, so the
      // switchTo/bindStream path never re-runs.
      applyToNamedConversation(event.conversationId, {
        boundAgentId: event.agentId,
        boundAgentName: event.agentName,
      });
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
        applyToConversation({ tokensUsed: event.totalTokens });
      }
      return;
    case "compaction_failed":
      // Compaction failed — history is unchanged. Remove the compaction_loading
      // block so the "Compacting…" shimmer disappears without leaving a marker.
      applyToConversation((s) => {
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
      applyToConversation({
        pendingUserMessages: [],
      });
      return;
    case "browser_action_request":
      // Embedded-browser action: fan out to the relay hook (which claims,
      // executes, posts the result). No store state; no-op without a relay.
      // Carry the delivering conversation so the relay claims/dispatches against
      // the session that issued the action — a background conversation can emit
      // one while a different conversation is on screen, and claiming at the
      // visible session would be rejected as an owner mismatch.
      emitBrowserActionRequest(event, sourceConversationId);
      return;
    case "session_status": {
      // Captured BEFORE the patch below adopts event.responseId, so a
      // running/waiting status carrying an unseen id marks a new turn.
      const prevResponseId = useChatStore.getState().activeResponse?.responseId;
      // The status patch is conversation-scoped; the cache/query side effects
      // further down are deliberately NOT (they are keyed by explicit id, so a
      // sub-agent's status still refreshes its parent's rail).
      applyToNamedConversation(event.conversationId, (s) => {
        // `sessionStatus` tracks the server's session-level status 1:1 — a
        // server `idle` means the session is idle, full stop, and the
        // "Working…" indicator (which reads only `sessionStatus`) turns off.
        // There is exactly one idle heuristic and it lives server-side (the
        // runner's PTY-activity watcher); the client must not second-guess it.
        // The bubble lifecycle below (`status`/`activeResponse`) still defers
        // to response_end, but that is separate from the session-level status.
        const patch: Partial<ChatState> = {
          sessionStatus: event.status,
          // Not sticky, unlike the background tally: every edge carries the
          // current reason (the runner re-attaches it to its own pane edges),
          // so an absent one means "no longer parked".
          blockedOn: event.blockedOn ?? null,
        };
        // The background-shell tally is STICKY. Only the Stop-hook-derived
        // status carries an authoritative count (the forwarder relabels its
        // `idle` to `waiting` and attaches `background_task_count`); the
        // PTY-activity watcher's running/idle edges carry none (`undefined`).
        // A claude-native turn that ends with shells still running emits, in
        // order: the Stop hook's `waiting`(+count), then — ~1s later, once the
        // pane quiesces — a bare PTY-activity `idle` (no count). If that
        // trailing `idle` reset the count the spinner would vanish a beat
        // after it appeared. So: an explicit count is authoritative (a Stop
        // hook's `0` clears it, so a finished shell drops the indicator on the
        // next turn end; a positive count sets it); `undefined` leaves it
        // untouched. A new `running` turn does NOT clear it — background shells
        // outlive turn boundaries, so the pill stays lit alongside the "Working…"
        // shimmer and the next Stop hook re-reports authoritatively. Only a
        // failure clears it (a dead session may never post another count to drop
        // a stale tally). Mirrors the server's `_publish_status`.
        if (event.backgroundTaskCount !== undefined) {
          patch.backgroundTaskCount = event.backgroundTaskCount;
          // Detail rides with the authoritative count on the same edge, so it
          // is authoritative too: a Stop hook that names its shells sets the
          // list; an older runner that sent count-only clears it to []. Keeps
          // the detail in lockstep with the number the pill shows.
          patch.backgroundTasks = event.backgroundTasks ?? [];
        } else if (event.status === "failed") {
          patch.backgroundTaskCount = 0;
          patch.backgroundTasks = [];
        }
        if (event.responseId !== undefined && event.status === "running") {
          patch.status = "streaming";
          patch.activeResponse = {
            responseId: event.responseId,
            state: "streaming",
            error: null,
          };
          // Blocks the reducer emitted before this edge named the turn
          // (codex opens reasoning ~2s earlier) carry no response id, so
          // they'd group into their own bubble beside the turn's own.
          // Attribute them to the turn they belong to.
          const adopted = adoptTrailingUnattributedBlocks(s.blocks, event.responseId);
          if (adopted !== null) patch.blocks = adopted;
        }
        // `waiting` is a TURN-END edge (the turn already finished; only
        // background work — background shells / sub-agents — outlives it). It
        // must finalize the local send lifecycle exactly like `idle`, NOT keep
        // it "streaming": the composer's send gate and "(queued)" placeholder
        // key off local `status`, so leaving it streaming would queue every new
        // message until the background work ends. The claude/cursor-native Stop
        // hook posts `waiting` WITH the ended turn's `response_id`, so it lands
        // here rather than via a bare PTY `idle`. `sessionStatus` stays
        // `waiting` (set above) and `backgroundTaskCount` is untouched, so the
        // "Working…" spinner and sidebar dot keep reflecting the background work.
        if (event.status === "idle" || event.status === "failed" || event.status === "waiting") {
          if (event.responseId !== undefined && s.activeResponse?.responseId === event.responseId) {
            patch.status = "idle";
            if (s.activeResponse.state !== "cancelled") {
              patch.activeResponse = {
                responseId: event.responseId,
                state: event.status === "failed" ? "failed" : "completed",
                error: null,
                completedAt: Date.now(),
              };
            }
          } else {
            // Terminal edge without a matching response id. This is the
            // NORMAL turn-end shape for most emitters — the status file's
            // bare `idle` and orchestration teardown carry none — so a
            // still-streaming turn is finalized here rather than left
            // "streaming" forever (which hid the settled turn's "Worked for"
            // fold and Fork action until a reload re-derived lifecycle from
            // the snapshot). Every terminal edge that reaches this point is
            // now a real turn end: control signals (policy deny, compaction)
            // no longer publish status. A `cancelled` turn is preserved as-is.
            patch.status = "idle";
            if (s.activeResponse?.state === "streaming") {
              patch.activeResponse = {
                ...s.activeResponse,
                state: event.status === "failed" ? "failed" : "completed",
                error: null,
                completedAt: Date.now(),
              };
            }
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
        // Surface terminal-native failures carried only by session status.
        // Deduplicate repeated status edges for one response, but preserve the
        // same failure on later turns so each rejected prompt has a visible error.
        const statusError = event.error;
        const hasMatchingStatusError =
          statusError != null &&
          s.blocks.some(
            (block) =>
              block.type === "error" &&
              block.ctx.responseId === (event.responseId ?? "") &&
              block.code === statusError.code &&
              block.message === statusError.message,
          );
        if (event.status === "failed" && statusError != null && !hasMatchingStatusError) {
          patch.blocks = [
            ...s.blocks,
            {
              type: "error",
              ctx: {
                agent: null,
                depth: 0,
                turn: 0,
                timestamp: 0,
                responseId: event.responseId ?? "",
                itemId: null,
              },
              message: statusError.message,
              source: "",
              code: statusError.code,
              ...structuredErrorFields(statusError),
            } satisfies ErrorBlock,
          ];
        }
        // A `runner_disconnected` card is an observation ("can't reach the
        // runner"), not a turn result: any live status edge from the runner
        // proves it is reachable again, so the stale card goes.
        if (event.status !== "failed") {
          const current = patch.blocks ?? s.blocks;
          const reachable = current.filter(
            (b) => !(b.type === "error" && b.code === "runner_disconnected"),
          );
          if (reachable.length !== current.length) patch.blocks = reachable;
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
      // Draining the queue is level-triggered (a React effect calls
      // maybeFlushQueuedHead on every status/queue change), NOT edge-triggered
      // here — a single "flush on the idle event" is fragile: a message queued
      // just after the idle edge, or an SSE reconnect that replays state
      // without a fresh transition, would strand the queue forever.
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
      //      the right entry. No text match: the native transcript
      //      reformats text (reply-quote `>` blockquotes, `[Attached:]`
      //      markers), so a text guard would wrongly skip the drop and
      //      strand the bubble as a duplicate. Only system markers, which
      //      never had a bubble here, are held back.
      //   3. No pending entry — render the event payload as a fresh
      //      committed bubble (TUI-typed message, marker, or another
      //      client).
      applyToConversation((s) => {
        if (hasCommittedItem(s.blocks, event.itemId)) {
          // The committed copy is already in `blocks` — the forwarder-mirrored
          // item beat this event through the stream, or a snapshot merge
          // inserted it. Still ack the optimistic bubble: returning without
          // dropping it strands a duplicate user bubble at the transcript tail.
          // Same precision order as below (named entry, then FIFO head), minus
          // the append.
          const cleared = event.clearedPendingId;
          const at = cleared ? s.pendingUserMessages.findIndex((p) => p.tempId === cleared) : -1;
          if (at >= 0) {
            return {
              pendingUserMessages: [
                ...s.pendingUserMessages.slice(0, at),
                ...s.pendingUserMessages.slice(at + 1),
              ],
            };
          }
          // FIFO-head fallback — same marker guard as the promote path below. A
          // mirrored system marker (the vendor CLI's own `[Request interrupted
          // by user]` record) is synthesized by the CLI, owns no pending entry,
          // and arrives with clearedPendingId unset; dropping the head would
          // steal a real queued message's bubble. Hold the head back for a marker.
          const eventContent = userContentFromEvent(event);
          if (eventContent !== null && isSystemUserContent(eventContent)) return {};
          if (s.pendingUserMessages.length === 0) return {};
          return { pendingUserMessages: s.pendingUserMessages.slice(1) };
        }

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
                  matched.createdAtS,
                ),
              ],
            };
          }
        }

        // 2. FIFO head fallback (id not adopted yet / cross-client).
        //    Skipped for a mirrored system marker (the vendor CLI's own
        //    interrupt record): it is synthesized by the CLI, never queued
        //    here, so popping the head would hand the queued message's
        //    uploads to the marker and leave the real message empty. A
        //    `[System: …]` notice DOES have a pending entry, but the server
        //    drains it and names it via `clearedPendingId`, so it lands on
        //    branch 1 and never reaches this fallback.
        const eventContent = userContentFromEvent(event);
        const head =
          eventContent !== null && isSystemUserContent(eventContent)
            ? undefined
            : s.pendingUserMessages[0];
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
                head.createdAtS,
              ),
            ],
          };
        }

        // 3. Nothing pending (or a marker that owns no bubble) — render the
        //    event payload fresh.
        if (eventContent === null) return {};
        return {
          blocks: [
            ...s.blocks,
            committedUserBlock(event.itemId, eventContent, undefined, event.createdBy),
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
      applyToConversation((s) => {
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
        applyToConversation((s) => {
          if (s.interruptedResponseIds.includes(interruptedResponseId)) return {};
          return {
            interruptedResponseIds: [...s.interruptedResponseIds, interruptedResponseId],
          };
        });
      }
      finalizeCurrentActive("cancelled", event.responseId, sourceConversationId);
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
    case "session_superseded":
      // The conversation we're viewing was rotated away (e.g. Claude
      // `/clear`): follow it to the new one. Guard on the active
      // conversation id so a late event from a stream we've already
      // switched away from can't yank the user, and ignore a self-target
      // no-op. `ChatPage` observes `redirectToConversationId` and performs
      // the actual react-router navigation.
      // Writes app-global state (`redirectToConversationId`) alongside
      // conversation state, so it keeps its own guard rather than going through
      // `applyToConversation`: the redirect only makes sense for the
      // conversation on screen, and a background conversation that gets
      // superseded should redirect when the user switches to it, not before.
      useChatStore.setState((s) => {
        if (s.conversationId !== event.conversationId) return {};
        if (event.targetConversationId === s.conversationId) return {};
        return { redirectToConversationId: event.targetConversationId };
      });
      // The rotation happened mid-input: the `/clear` (or whatever the user just
      // sent) never gets a `session.input.consumed` on THIS conversation — the
      // runner moved to the new one — so its optimistic bubble would otherwise
      // spin forever. Drop it; resuming starts a fresh turn.
      applyToNamedConversation(event.conversationId, { pendingUserMessages: [] });
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
    case "session_model_options":
      // A runner-owned native model catalog just resolved. Refetch the
      // cache-warmed snapshot so the picker and any delayed sticky handoff
      // use the same authoritative options.
      void refetchRunnerBackedSessionState(event.conversationId, {
        modelOptionsResolved: true,
      });
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
      applyToConversation((s) => {
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
  // Background shells outliving a turn do NOT light the row: the server
  // delivers that turn-end as `idle` (it takes a new message right away), and
  // the in-chat indicator still reports the shells from the tally.
  const working = sessionStatus === "running" || sessionStatus === "waiting";
  const listStatus: NonNullable<Conversation["status"]> =
    sessionStatus === "failed" ? "failed" : working ? "running" : "idle";
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
  if (child.task_summary !== undefined) patch.task_summary = strOrNull(child.task_summary);
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
      task_summary: patch.task_summary ?? null,
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

/**
 * Apply `session.*` side effects for each event, then pass it through.
 *
 * `conversationId` is the stream's own conversation — the only reliable routing
 * key, since several events carry no id in their payload. It also keys the raw
 * SSE debug log the execution-logs panel reads.
 */
async function* tapSessionEvents(
  events: AsyncIterable<StreamEvent>,
  conversationId: string,
): AsyncIterable<StreamEvent> {
  for await (const event of events) {
    handleSessionEvent(event, conversationId);
    pushSseEvent(conversationId, event);
    yield event;
  }
}

/**
 * Force a conversation's `activeResponse` into a terminal state without
 * needing a closure-scoped setter. Mirrors `finalizeActive` but works from the
 * module-scope `handleSessionEvent` boundary.
 *
 * :param conversationId: the conversation whose stream reported this, or
 *     ``null`` to target whichever is active. `activeResponse` is
 *     conversation-scoped, so this lands on that conversation's entry —
 *     a background interrupt settles its OWN turn and leaves the visible
 *     one alone.
 */
function finalizeCurrentActive(
  state: ActiveResponse["state"],
  responseIdOverride?: string,
  conversationId?: string | null,
): void {
  const target = conversationId ?? useChatStore.getState().conversationId;
  setterFor(target)((s) => {
    if (s.activeResponse === null && responseIdOverride === undefined) return {};
    const responseId = s.activeResponse?.responseId ?? responseIdOverride ?? "";
    return {
      activeResponse: { responseId, state, error: null, completedAt: Date.now() },
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
      activeResponse: { responseId, state, error, completedAt: Date.now() },
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
