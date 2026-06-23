// Mirrors sdks/python-client/omnigent_client/_events.py.
//
// Hand-ported. When _events.py changes, update this file and the
// matching reducer logic in blockStream.ts. See ap-web/README.md
// "Reducer parity" for the workflow.
//
// Naming: Python uses snake_case fields + PascalCase class names; TS
// uses camelCase fields + a `type` discriminator string equal to the
// Python class name lowercased (e.g. ResponseCreated → "response_created").

import type { ErrorInfo, ModelUsage, RememberScope, Response, SandboxLaunchStage } from "./types";

/** Provider-native tool item types. */
export const NATIVE_TOOL_TYPES = new Set<string>([
  "web_search_call",
  "file_search_call",
  "code_interpreter_call",
  "computer_call",
  "image_generation_call",
  "mcp_call",
  "mcp_list_tools",
]);

/**
 * JSON-RPC method name MCP uses for elicitation requests. The
 * server's `response.elicitation_request` SSE event carries this
 * verbatim under `method` so MCP-aware consumers can route on the
 * same name they already recognize.
 */
export const MCP_ELICITATION_METHOD = "elicitation/create";

// ── Response lifecycle events ────────────────────────────

/** `response.created` — always first (sequence 0). */
export interface ResponseCreated {
  type: "response_created";
  response: Response;
}

/** `response.queued` — only when `background=true`. */
export interface ResponseQueued {
  type: "response_queued";
  response: Response;
}

/** `response.in_progress` — execution started. */
export interface ResponseInProgress {
  type: "response_in_progress";
  response: Response;
}

/** `response.completed` — agent finished successfully. */
export interface ResponseCompleted {
  type: "response_completed";
  response: Response;
}

/** `response.failed` — unrecoverable error. */
export interface ResponseFailed {
  type: "response_failed";
  response: Response;
}

/** `response.incomplete` — stopped early. */
export interface ResponseIncomplete {
  type: "response_incomplete";
  response: Response;
  /** "max_iterations", "execution_timeout", etc. */
  reason: string;
}

/** `response.cancelled` — cancelled via POST /cancel. */
export interface ResponseCancelled {
  type: "response_cancelled";
  response: Response;
}

// ── Text streaming ───────────────────────────────────────

/** `response.output_text.delta` — incremental text token. */
export interface TextDelta {
  type: "text_delta";
  delta: string;
  /**
   * For terminal-observed live streaming (claude-native), the vendor's
   * stable per-assistant-message id. Lets the store scope an in-flight
   * buffer to one message and reconcile it against the final item.
   * `undefined` for ordinary in-process task streaming, where deltas
   * group under the active response instead.
   */
  messageId?: string;
  /** 0-based chunk order within `messageId`. `undefined` when not native streaming. */
  index?: number;
  /** `true` on the last chunk for `messageId`. `undefined` when not native streaming. */
  final?: boolean;
}

// ── Reasoning ────────────────────────────────────────────

/** `response.reasoning.started` — reasoning block opened. */
export interface ReasoningStarted {
  type: "reasoning_started";
}

/** `response.reasoning_text.delta` — reasoning token. */
export interface ReasoningDelta {
  type: "reasoning_delta";
  delta: string;
}

/** `response.reasoning_summary_text.delta` — summary token. */
export interface ReasoningSummaryDelta {
  type: "reasoning_summary_delta";
  delta: string;
}

// ── Parsed output items ─────────────────────────────────

/** A tool call from `output_item.done` (type `function_call`). */
export interface ToolCall {
  type: "tool_call";
  name: string;
  arguments: Record<string, unknown>;
  callId: string;
  /** "completed" | "action_required" | "incomplete". */
  status: string;
  /** "coder" or "coder.researcher" — agent that invoked the tool. */
  agentName: string;
  /** Server-assigned item id (`event.item.id`). Empty when not supplied. */
  itemId: string;
  /** Server-assigned response id (`event.item.response_id`). */
  responseId: string;
}

/** A tool result from `output_item.done` (type `function_call_output`). */
export interface ToolResult {
  type: "tool_result";
  callId: string;
  output: string;
  itemId: string;
  responseId: string;
}

/**
 * A server-initiated elicitation, MCP shape.
 *
 * Parsed out of the `response.elicitation_request` SSE event; the
 * event's `params` block matches MCP's `ElicitRequestFormParams`
 * field-for-field, plus extras (`phase`, `policyName`,
 * `contentPreview`) under MCP's `extra="allow"` config.
 *
 * Consumers respond via `POST /v1/sessions/{sessionId}/events` with
 * type `approval`, `elicitation_id`, and MCP-shape `ElicitResult`
 * fields (`action` + optional `content`).
 */
/** A policy denied the user input or LLM call. */
export interface PolicyDenied {
  type: "policy_denied";
  reason: string;
  phase: string;
}

export interface ElicitationRequest {
  type: "elicitation_request";
  elicitationId: string;
  /**
   * Session that owns the parked elicitation Future. Usually the
   * active session; set to a child session id when the server mirrors
   * a sub-agent prompt into its parent chat.
   */
  targetSessionId?: string | null;
  message: string;
  /** A restricted subset of JSON Schema. Empty `{}` for binary approve/reject. */
  requestedSchema: Record<string, unknown>;
  /** MCP elicitation mode — ``"form"`` (inline) or ``"url"`` (standalone page). */
  mode: string;
  /** Standalone approval page URL when ``mode === "url"``. */
  url?: string | null;
  /** Producer-supplied extra (policy ASK only): "input" | "tool_call" | "tool_result" | "output". */
  phase: string;
  /** Producer-supplied extra (policy ASK only): name of the deciding policy. */
  policyName: string;
  /** Producer-supplied extra (policy ASK only): truncated snapshot of the gated content. */
  contentPreview: string;
  /**
   * Producer-supplied extra (claude-native only): structured
   * AskUserQuestion payload — present when the gated tool is
   * Claude Code's built-in ``AskUserQuestion``. The UI's
   * ApprovalCard reads this in preference to parsing the
   * (truncated) ``contentPreview`` JSON string.
   *
   * Shape: ``{questions: [{question, header?, options:
   * [{label, description?}], multiSelect}]}`` — same the
   * :file:`@/lib/askUserQuestion` helper produces.
   *
   * Optional/null for any other elicitation (policy ASK,
   * PermissionRequest for non-AskUserQuestion tools).
   */
  askUserQuestion?: Record<string, unknown> | null;
  /**
   * Producer-supplied extra (claude-native only): the FULL
   * ``ExitPlanMode`` tool_input, untruncated — present when the
   * gated tool is Claude Code's built-in ``ExitPlanMode``. The
   * UI's ApprovalCard renders the ``plan`` markdown as a
   * plan-review card with approve / approve-in-auto-mode /
   * reject-with-feedback actions.
   *
   * Passed through verbatim from Claude's hook payload (no field
   * filtering server-side), so the shape tracks Claude Code builds:
   * ``{plan: string, planFilePath?: string, allowedPrompts?: [...]}``.
   *
   * Optional/null for any other elicitation.
   */
  exitPlanMode?: Record<string, unknown> | null;
  /**
   * Producer-supplied extra (codex-native command approvals only):
   * structured command details that let the UI render the approval
   * without dumping the full Codex JSON-RPC payload.
   */
  codexCommand?: {
    command: string;
    cwd: string | null;
    reason: string | null;
    execPolicyAmendment: string[] | null;
  } | null;
  /**
   * Producer-supplied extra (claude-native edit-tool prompts only):
   * when true, the PermissionRequest endpoint is gating a file-editing
   * tool (Edit/Write/MultiEdit/NotebookEdit) under a mode that still
   * prompts. The UI's ApprovalCard renders an "Accept & allow all
   * edits" button that, on accept, asks the server to switch the
   * session into Claude Code's ``acceptEdits`` mode — the web
   * equivalent of the native shift+tab "auto-accept edits" toggle.
   *
   * Absent/false for every other elicitation (non-edit tools, codex,
   * policy ASK, claude-sdk), so the button only appears where the
   * mode switch is meaningful.
   */
  allowAllEdits?: boolean;
  /**
   * Producer-supplied extra (claude-native non-edit tool prompts only):
   * present when the PermissionRequest endpoint is gating a tool that
   * supports a persistent "don't ask again" allow rule (everything
   * except edit tools, ExitPlanMode, and AskUserQuestion). ``tool`` is
   * the gated tool name; ``host`` is the WebFetch request domain when
   * present. The UI's ApprovalCard renders an "Approve & don't ask
   * again for <host|tool>" button that, on accept, asks the server to
   * install a session-scoped allow rule — the web equivalent of the
   * native TUI's "don't ask again" permission option.
   *
   * Absent/null for every other elicitation (edit tools, ExitPlanMode,
   * AskUserQuestion, codex, policy ASK), so the button only appears
   * where the allow rule is meaningful.
   */
  rememberScope?: RememberScope | null;
}

/**
 * Server signal that an outstanding elicitation has been cleared
 * (parsed from `response.elicitation_resolved`). The chat-store
 * matches by id rather than scanning for first-pending. Tool/result
 * events are deliberately not used as a fallback because they do not
 * carry an elicitation id.
 */
export interface ElicitationResolved {
  type: "elicitation_resolved";
  elicitationId: string;
}

/** A provider-native tool output (web_search, mcp, etc.). */
export interface NativeToolCall {
  type: "native_tool_call";
  /** e.g. "web_search_call". */
  toolType: string;
  data: Record<string, unknown>;
  itemId: string;
  responseId: string;
}

/** The final assistant message from `output_item.done` (type `message`). */
export interface MessageDone {
  type: "message_done";
  content: Array<Record<string, unknown>>;
  itemId: string;
  responseId: string;
}

/**
 * Slash-command item from `output_item.done` (type `slash_command`).
 * Lifted from `SlashCommandItem`; reducer produces a `SlashCommandBlock`.
 */
export interface SlashCommand {
  type: "slash_command";
  /**
   * `"skill"` for plugin/Skill invocations, `"command"` for surfaced
   * CLI built-ins (`/effort`, `/clear`, `/compact`, `/model`,
   * `/ultrareview`). The renderer uses this to pick the prefix label
   * and icon.
   */
  kind: "skill" | "command";
  /** Command name with leading `/` stripped. */
  name: string;
  /** Raw `<command-args>` text; empty when none. */
  arguments: string;
  /** `<local-command-stdout>` text, or `null` when none. */
  output: string | null;
  /** Harness/agent name that observed the command. */
  agentName: string;
  /** Human author email when the server attributed one; carried onto
   * the synthesized user-echo bubble for shared-session labels. */
  createdBy?: string;
  itemId: string;
  responseId: string;
}

/**
 * Terminal command item from `output_item.done` (type `terminal_command`).
 * Produced by the claude-native transcript forwarder when the user types `!cmd`.
 */
export interface TerminalCommandEvent {
  type: "terminal_command";
  /** `"input"` for the command line; `"output"` for stdout/stderr. */
  kind: "input" | "output";
  input: string | null;
  stdout: string | null;
  stderr: string | null;
  itemId: string;
  responseId: string;
}

// ── File output ──────────────────────────────────────────

/** `response.output_file.done` — file artifact produced. */
export interface OutputFileDone {
  type: "output_file_done";
  fileId: string;
  filename: string | null;
  contentType: string | null;
}

// ── Error and retry ──────────────────────────────────────

/** `response.retry` — a retryable failure, will retry. */
export interface RetryEvent {
  type: "retry";
  /** "llm" or "tool". */
  source: string;
  toolName: string | null;
  attempt: number;
  maxAttempts: number;
  delaySeconds: number;
  error: ErrorInfo;
}

/** `response.error` — an error during execution. */
export interface ErrorEvent {
  type: "error";
  /** "llm" or "tool". */
  source: string;
  toolName: string | null;
  error: ErrorInfo;
}

// ── Compaction ───────────────────────────────────────────

/** `response.compaction.in_progress` — server started compacting. */
export interface CompactionInProgress {
  type: "compaction_in_progress";
}

/** `response.compaction.completed` — compaction finished successfully. */
export interface CompactionCompleted {
  type: "compaction_completed";
  /**
   * Tiktoken estimate of the post-compaction context size in tokens.
   * `null` when unavailable. Use to update the context-ring without
   * waiting for the next `response.completed` usage report.
   */
  totalTokens: number | null;
}

/** `response.compaction.failed` — compaction failed; history unchanged. */
export interface CompactionFailed {
  type: "compaction_failed";
}

// ── Async client-tool cancel ─────────────────────────────

/**
 * `response.client_task.cancel` — server-to-client cancel
 * notification for async client-tool dispatches.
 */
export interface ClientTaskCancel {
  type: "client_task_cancel";
  taskId: string;
  callId: string | null;
}

// ── Session lifecycle events (session.*) ─────────────────
//
// Emitted on `GET /v1/sessions/{id}/stream`. Wire shapes vary —
// see `omnigent/server/schemas.py` for canonical Pydantic models:
//
// - `session.status` / `session.created` /
//   `session.resource.created` / `session.resource.deleted` use a
//   FLAT envelope (`{type, ...fields}`).
// - `session.input.consumed` / `session.interrupted` use a NESTED
//   envelope (`{type, data: {...}}`).
//
// The TS interfaces below already lift the flat/nested distinction
// into typed fields, so consumers don't need to care.

/**
 * `session.status` — session lifecycle transition.
 *
 * `launching` means a task/session exists but has not emitted a concrete
 * harness-start signal. `waiting` arrives when the parent agent loop parks
 * on background tools / sub-agents. The session snapshot's `status` never
 * reports `waiting`; it is live-only.
 */
export interface SessionStatusEvent {
  type: "session_status";
  conversationId: string;
  status: "idle" | "launching" | "running" | "waiting" | "failed";
  responseId?: string;
}

/**
 * `session.usage` — token-usage update from a terminal-backed runtime.
 *
 * Substitutes for `response.completed` on claude-native sessions
 * (which never emit one). Either field may be absent; treat missing
 * fields as "no change" and keep the cached value.
 *
 * - `contextTokens`: input + cache_creation + cache_read from the
 *   latest Claude `message.usage`.
 * - `contextWindow`: 200k normally, 1M with `opus[1m]`/`sonnet[1m]`.
 * - `totalCostUsd`: cumulative session spend in USD after this update
 *   (server-computed, the cost-budget total). Present only when the
 *   session is priced; absent on an unpriced session or a broadcast
 *   carrying no cost change, in which case the cached value is kept.
 * - `usageByModel`: per-model breakdown of the subtree usage, keyed by
 *   raw harness model id; absent on a broadcast carrying no per-model
 *   change (the client keeps its cached map).
 */
export interface SessionUsageEvent {
  type: "session_usage";
  conversationId: string;
  contextTokens?: number;
  contextWindow?: number;
  totalCostUsd?: number;
  usageByModel?: Record<string, ModelUsage>;
}

/**
 * `session.model` — active-model switch from a claude-native session.
 *
 * Emitted by the Omnigent server when the claude-native forwarder observes a
 * `/model` change made inside the Claude Code terminal (a typed command
 * or the in-TUI picker). Carries the tier alias (`"fable"` / `"opus"` /
 * `"sonnet"` / `"haiku"`) the session is now on so the model picker
 * reflects it.
 */
export interface SessionModelEvent {
  type: "session_model";
  conversationId: string;
  model: string;
}

/**
 * `session.reasoning_effort` — active thinking-level switch from a native
 * session.
 *
 * Emitted by the Omnigent server after a native wrapper reports an effort
 * change observed outside the Web UI: Claude-native mirrors terminal/TUI
 * changes, and Codex-native mirrors Codex app-server/config state. Carries
 * `null` when the native runtime cleared back to its model default.
 */
export interface SessionReasoningEffortEvent {
  type: "session_reasoning_effort";
  conversationId: string;
  reasoningEffort: string | null;
}

/**
 * `session.collaboration_mode` — active Codex collaboration-mode switch.
 *
 * Emitted when a Codex-native thread enters or exits Plan mode, whether from
 * the web UI toggle or from the native Codex TUI.
 */
export interface SessionCollaborationModeEvent {
  type: "session_collaboration_mode";
  conversationId: string;
  mode: string;
}

/**
 * `session.agent_changed` — the session's bound agent was switched in
 * place (switch-agent route).
 *
 * The harness may have changed family (e.g. claude-sdk → claude-native),
 * which flips the session's message lifecycle: native sessions defer
 * user-message persistence to the transcript round-trip. Clients must
 * re-derive their cached session state from a fresh snapshot — acting on
 * the stale binding drops the first post-switch optimistic bubble.
 */
export interface SessionAgentChangedEvent {
  type: "session_agent_changed";
  conversationId: string;
  agentId: string;
  agentName: string;
}

/**
 * `session.todos` — todo-list update from a claude-native session.
 *
 * Emitted by the Omnigent server when the claude-native forwarder receives
 * a `PostToolUse`/`TodoWrite` hook event from Claude Code. Clients
 * should replace their cached todo list entirely on each event (the
 * payload is the full current list, not a diff).
 *
 * Each todo item has:
 * - `content`: the task description string
 * - `status`: `"pending"` | `"in_progress"` | `"completed"`
 * - `activeForm`: present-continuous form of the task (e.g. `"Running tests"`).
 *   Shown by the TodoPanel under in-progress items when distinct from `content`.
 */
export interface SessionTodosEvent {
  type: "session_todos";
  conversationId: string;
  todos: Array<{
    content: string;
    status: "pending" | "in_progress" | "completed";
    activeForm: string;
  }>;
}

/**
 * `session.terminal_pending` — the runner is auto-creating (or has
 * finished/failed creating) the terminal for a terminal-first session
 * (claude-native / codex-native). Drives the spinner on the Terminal
 * pill so a spinning-up terminal reads as "loading" rather than a
 * silent greyed-out button. `pending: false` clears the spinner; from
 * then on the UI relies purely on whether a terminal resource exists.
 */
export interface SessionTerminalPendingEvent {
  type: "session_terminal_pending";
  conversationId: string;
  pending: boolean;
}

/**
 * `session.sandbox_status` — the session's managed-sandbox launch
 * advanced a stage (provision → clone → host start → runner connect),
 * settled successfully (`ready`), or failed (`failed` + `error`).
 * Drives the provisioning indicator on the session page so the
 * background launch reads as live progress instead of a dead chat.
 */
export interface SessionSandboxStatusEvent {
  type: "session_sandbox_status";
  conversationId: string;
  stage: SandboxLaunchStage;
  /** Failure detail when `stage === "failed"`; `null` otherwise. */
  error: string | null;
}

/**
 * `session.input.consumed` — a queued input item was persisted into
 * conversation history. Used to backfill optimistic user-bubble
 * `itemId`s with the server-assigned id. Does NOT carry
 * `response_id`; response-id grouping continues to come from
 * `response.created`.
 */
export interface SessionInputConsumedEvent {
  type: "session_input_consumed";
  itemId: string;
  /** Item-type discriminator, e.g. `"message"`, `"function_call_output"`. */
  itemType: string;
  /** True when the persisted item is durable hidden context. */
  isMeta?: boolean;
  /**
   * Human author email, when known. Absent for agent/tool/system items
   * and single-user sends. Threaded onto the bubble for live attribution.
   */
  createdBy?: string;
  /** Decoded item payload — heterogeneous, `itemType`-specific. */
  data: Record<string, unknown>;
  /**
   * When this consumed message drained a server pending-input entry
   * (a native-terminal web message round-tripping back from the
   * transcript), the drained entry's id, e.g. ``"pending_a1b2c3"``.
   * Lets the store drop the matching optimistic bubble by id instead
   * of by FIFO position. Absent for non-native messages and messages
   * that matched no pending entry (e.g. typed directly in the TUI).
   */
  clearedPendingId?: string | null;
}

/**
 * `session.interrupted` — user-triggered cancel reached the loop.
 *
 * Co-emitted with `response.incomplete` (whose underlying
 * `incompleteDetails.reason == "user_interrupt"`). Prefer this
 * event for "user interrupted" decoration; it distinguishes
 * explicit interrupt from a generic `AbortError` on the wire.
 */
export interface SessionInterruptedEvent {
  type: "session_interrupted";
  /** Unix epoch seconds when the interrupt request reached the server. */
  requestedAt: number;
  /** Response id interrupted by a terminal-backed integration, when known. */
  responseId?: string;
}

/**
 * `session.created` — a child (sub-agent) session was spawned.
 *
 * Rides on the PARENT session's stream. `conversationId` is the
 * parent; `childSessionId` is the new child. Consumers can pivot
 * to subscribe to the child's stream without polling history.
 * Sub-agent rendering is out of scope for the sessions migration;
 * until that lands this event is a no-op.
 */
export interface SessionCreatedEvent {
  type: "session_created";
  /** Parent session id — the carrier stream's conversation id. */
  conversationId: string;
  /** Newly-created child session id. */
  childSessionId: string;
  /** Registered agent id the child runs as. May be missing on legacy spawn paths. */
  agentId: string | null;
  /** Echo of `conversationId` for forward-compat. May be absent on the wire. */
  parentSessionId: string | null;
}

/**
 * Session resource record carried by `session.resource.*` events.
 *
 * Resource-specific payload lives in `metadata`; terminal/resource
 * consumers can narrow `type` in their own layer without coupling the
 * generic SSE parser to terminal UI details.
 */
export interface SessionResource {
  id: string;
  type: string;
  name: string;
  /**
   * Owning session/conversation id (wire ``session_id``; kept snake to
   * mirror the resource passthrough, like ``metadata``'s keys). Needed
   * by cache updaters (``applyTerminalCreated``) to target the right
   * query key — without it the created-event handler early-returns and
   * the terminal never appears live.
   */
  session_id: string;
  metadata: Record<string, unknown>;
}

/**
 * `session.resource.created` — a session-scoped resource was created.
 *
 * Carries the full resource record so consumers can update local
 * caches without a follow-up REST read.
 */
export interface SessionResourceCreatedEvent {
  type: "session_resource_created";
  resource: SessionResource;
}

/**
 * `session.resource.deleted` — a session-scoped resource was deleted.
 */
export interface SessionResourceDeletedEvent {
  type: "session_resource_deleted";
  resourceId: string;
  resourceType: string;
  sessionId: string;
}

/**
 * `session.child_session.updated` — a child (sub-agent) session's status
 * changed, pushed to the PARENT's stream. Carries the full child summary
 * so the child-sessions cache can be patched without a refetch.
 */
export interface SessionChildSessionUpdatedEvent {
  type: "session_child_session_updated";
  /** Parent (carrier) session id. */
  conversationId: string;
  childSessionId: string;
  /** ChildSessionSummary-shaped record; mapped by the store handler. */
  child: Record<string, unknown>;
}

/**
 * `session.changed_files.invalidated` — the session's changed-files list
 * may have changed; the consumer should refetch it. Coarse signal (no
 * per-file payload).
 */
export interface SessionChangedFilesInvalidatedEvent {
  type: "session_changed_files_invalidated";
  sessionId: string;
  environmentId: string;
}

/**
 * `session.terminal.activity` — a terminal's pane produced output
 * (runner-determined). Drives the "active" badge with no client PTY
 * attach. A transient pulse, not persisted.
 */
export interface SessionTerminalActivityEvent {
  type: "session_terminal_activity";
  sessionId: string;
  terminalId: string;
}

/**
 * `session.skills` — the session's runner-owned skills just resolved
 * (the server's background fetch populated its per-session skills cache).
 * Skills are fetched off the snapshot hot path, so the snapshot serves
 * an empty list until the fetch lands; this event is the "skills are
 * ready, re-read the snapshot" nudge. Consumers refetch the session
 * snapshot and apply its now-populated `skills` to fill the composer's
 * slash-command menu. Carries no payload beyond the conversation id.
 */
export interface SessionSkillsEvent {
  type: "session_skills";
  conversationId: string;
}

/**
 * `session.model_options` — the Codex app-server model catalog just
 * resolved for a session. Consumers refetch the session snapshot and apply
 * its now-populated `codexModelOptions`.
 */
export interface SessionModelOptionsEvent {
  type: "session_model_options";
  conversationId: string;
}

/** One user currently viewing the session (holding its stream open). */
export interface SessionViewer {
  /** Authenticated identity, e.g. `"alice@example.com"`. */
  userId: string;
  /** ISO 8601 UTC join time; stable across grace-window reconnects. */
  joinedAt?: string;
  /** True when every tab the user holds is backgrounded (greyed avatar). */
  idle: boolean;
}

/**
 * `session.presence` — the session's viewer list changed. FULL state,
 * not a delta: consumers replace their viewer list wholesale, so missed
 * events self-heal on the next event or reconnect snapshot. Includes
 * the receiving user themself (filtered out for display).
 */
export interface SessionPresenceEvent {
  type: "session_presence";
  conversationId: string;
  viewers: SessionViewer[];
}

// ── Union type for all events ────────────────────────────

export type StreamEvent =
  | ResponseCreated
  | ResponseQueued
  | ResponseInProgress
  | ResponseCompleted
  | ResponseFailed
  | ResponseIncomplete
  | ResponseCancelled
  | TextDelta
  | ReasoningStarted
  | ReasoningDelta
  | ReasoningSummaryDelta
  | ToolCall
  | ToolResult
  | NativeToolCall
  | SlashCommand
  | TerminalCommandEvent
  | MessageDone
  | OutputFileDone
  | RetryEvent
  | ErrorEvent
  | CompactionInProgress
  | CompactionCompleted
  | CompactionFailed
  | ClientTaskCancel
  | ElicitationRequest
  | ElicitationResolved
  | PolicyDenied
  | SessionStatusEvent
  | SessionUsageEvent
  | SessionModelEvent
  | SessionReasoningEffortEvent
  | SessionCollaborationModeEvent
  | SessionAgentChangedEvent
  | SessionTodosEvent
  | SessionTerminalPendingEvent
  | SessionSandboxStatusEvent
  | SessionInputConsumedEvent
  | SessionInterruptedEvent
  | SessionCreatedEvent
  | SessionResourceCreatedEvent
  | SessionResourceDeletedEvent
  | SessionChildSessionUpdatedEvent
  | SessionChangedFilesInvalidatedEvent
  | SessionTerminalActivityEvent
  | SessionSkillsEvent
  | SessionModelOptionsEvent
  | SessionPresenceEvent;
