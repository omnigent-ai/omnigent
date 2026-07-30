// Mirrors sdks/python-client/omnigent_client/_blocks.py.
//
// Hand-ported. When _blocks.py changes, update this file and the
// matching reducer logic in blockStream.ts. See web/README.md
// "Reducer parity" for the workflow.
//
// Naming: Python uses snake_case fields + PascalCase class names; TS
// uses camelCase fields + a `type` discriminator string equal to the
// Python class name lowercased (e.g. ResponseStartBlock → "response_start").

import type { RememberScope, Response } from "./types";

/**
 * Metadata attached to every stream block.
 *
 * - `agent`: name of the agent that produced this block, e.g.
 *   `"coder.researcher"`. `null` for the root agent.
 * - `depth`: nesting depth of the agent in the sub-agent tree
 *   (count of `.` in `agent`, or 0 when `agent` is null).
 * - `turn`: turn number within the current response.
 * - `timestamp`: monotonic-ish timestamp (seconds) when the block was created.
 * - `responseId`: server-assigned response id this block belongs to.
 *   Empty string for synthetic blocks not associated with a server
 *   response yet (e.g. an optimistic user-message block before
 *   `response.created` arrives).
 * - `itemId`: server-assigned item id this block originated from. Set
 *   on blocks derived from `response.output_item.done` (tool calls,
 *   native tools, finalized assistant messages) and on every block
 *   produced by `itemsToBlocks` (historical hydration). `null` for
 *   ephemeral blocks (text/reasoning chunks emitted before their item
 *   is finalized, lifecycle markers like `response_start`).
 * - `createdBy`: email of the human author. Omitted for agent/tool/system
 *   items and older history.
 */
export interface BlockContext {
  agent: string | null;
  depth: number;
  turn: number;
  timestamp: number;
  responseId: string;
  itemId: string | null;
  createdBy?: string;
}

/** Per-message-item content blocks. Both user input and assistant output. */
export type MessageContentBlock =
  | { type: "input_text"; text: string }
  | { type: "input_image"; file_id: string; filename?: string }
  | { type: "input_file"; file_id: string; filename?: string }
  | { type: "output_text"; text: string };

/** A single tool call paired with its result. Mirrors `ToolExecution`. */
export interface ToolExecution {
  name: string;
  arguments: Record<string, unknown>;
  argsSummary: string;
  callId: string;
  agentName: string;
  /** "server" or "client". */
  executedBy: "server" | "client";
  /** Tool output text, or `null` if not yet available. */
  output: string | null;
}

// ── Response lifecycle ───────────────────────────────────

/** The response has started. */
export interface ResponseStartBlock {
  type: "response_start";
  ctx: BlockContext;
  /** Agent model name, e.g. "coder". */
  model: string;
  /** Server-assigned response ID. */
  responseId: string;
  /**
   * Server-assigned conversation ID. Populated from
   * `event.response.conversation.id` on the `response.created` SSE
   * event — this is the earliest the server tells us which
   * conversation the response belongs to. Null on synthetic blocks or
   * paths where the server doesn't include the conversation field.
   */
  conversationId: string | null;
}

// ── User input (web only — not in the Python SDK) ─────

/**
 * A user message item, surfaced as a block.
 *
 * Emitted by `itemsToBlocks` when hydrating history from
 * `/v1/conversations/:id/items`, and by the chat store at send-time
 * as an optimistic insert so the user's bubble appears instantly.
 * The session SSE stream does NOT emit `user_message` blocks — the
 * `session.input.consumed` event fires when the server persists the
 * input, but the store consumes it to backfill the optimistic block's
 * `itemId` rather than producing a new block. Result: exactly one
 * `user_message` block per send, owned by the optimistic insert and
 * backfilled in place.
 *
 * This block exists in web only; the Python SDK's `BlockStream`
 * never produces it (its consumers — terminal frontends — render the
 * user input directly, not as a block).
 */
export interface UserMessageBlock {
  type: "user_message";
  ctx: BlockContext;
  /** Same shape as `MessageItem.content` from the items API. */
  content: MessageContentBlock[];
  /**
   * Stable React key for the rendered bubble, set ONLY when this block
   * was promoted from an optimistic `pendingUserMessages` entry on
   * `session.input.consumed`. It carries that entry's client temp id so
   * the bubble keeps the SAME key across the optimistic→committed swap —
   * otherwise the key would change (`user:pend_N` → `user:item_id`),
   * remounting the node and producing a visible flink on every message.
   * Absent on blocks hydrated from history (`items`), which mount fresh.
   */
  stableKey?: string;
}

// ── Tool calls ───────────────────────────────────────────

/** A batch of tool calls from one iteration. */
export interface ToolGroup {
  type: "tool_group";
  ctx: BlockContext;
  executions: ToolExecution[];
  iteration: number;
}

/** A tool result, emitted after the tool executes. */
export interface ToolResultBlock {
  type: "tool_result";
  ctx: BlockContext;
  name: string;
  callId: string;
  agentName: string;
  output: string;
}

/** A provider-native tool output (web_search, mcp, etc.). */
export interface NativeToolBlock {
  type: "native_tool";
  ctx: BlockContext;
  /** Provider tool type, e.g. "web_search_call". */
  toolType: string;
  /** Human-readable label for display, e.g. "search". */
  label: string;
  /** Raw provider data dict. */
  data: Record<string, unknown>;
}

/**
 * A Claude Code slash-command invocation from the embedded TUI.
 * Rendered as a compact "skill invoked" row. web only — the
 * Python SDK's BlockStream has no corresponding event (terminal
 * frontends render slash commands via their own TUI).
 */
export interface SlashCommandBlock {
  type: "slash_command";
  ctx: BlockContext;
  /**
   * `"skill"` for plugin/Skill invocations, `"command"` for surfaced
   * CLI built-ins (`/effort`, `/clear`, `/compact`, `/model`,
   * `/ultrareview`). Drives the card's prefix label and icon.
   */
  kind: "skill" | "command";
  /** Command name with leading `/` stripped, e.g. `dev-productivity:simplify`. */
  name: string;
  /** Raw `<command-args>` text; empty when invoked with no args. */
  arguments: string;
  /** `<local-command-stdout>` text, or null when absent. */
  output: string | null;
}

/**
 * Reconstruct the literal composer text a skill invocation came from,
 * e.g. `/review-pr 123 focus on auth`. A skill's `slash_command`
 * receipt is the only transcript record of the user's send (the typed
 * text is consumed into a hidden `<skill>` meta message), so both
 * block funnels re-materialize it as a user bubble next to the
 * Skill indicator.
 */
export function slashCommandEchoText(name: string, args: string): string {
  return args ? `/${name} ${args}` : `/${name}`;
}

/**
 * Item id for the synthesized user-echo block. Derived from the
 * receipt's id (not invented per arrival) so the live-stream copy and
 * the snapshot/history copy of the same receipt dedupe by
 * `ctx.itemId` on reconcile.
 */
export function slashCommandEchoItemId(slashItemId: string): string {
  return `${slashItemId}:user`;
}

/**
 * An intelligent-model-router decision, rendered as a standalone muted
 * chip at its transcript position (turn start). Display-only — the
 * server keeps the matching `routing_decision` item out of the model's
 * history.
 */
export interface RoutingDecisionBlock {
  type: "routing_decision";
  ctx: BlockContext;
  /** Model id the router chose, e.g. `databricks-claude-opus-4-8`. */
  model: string;
  /** `true` when the brain ran on `model`; `false` = "would have picked". */
  applied: boolean;
  /** The router's one-line rationale; empty string when absent. */
  rationale: string;
  /** Sub-agent name when mirrored into the parent session; undefined otherwise. */
  agent?: string;
}

export interface TerminalCommandBlock {
  type: "terminal_command";
  ctx: BlockContext;
  /** `"input"` for the command line; `"output"` for stdout/stderr. */
  kind: "input" | "output";
  /** The raw command string; set when `kind="input"`. */
  input: string | null;
  /** Captured stdout; set when `kind="output"`. */
  stdout: string | null;
  /** Captured stderr; set when `kind="output"`. */
  stderr: string | null;
}

// ── Text ─────────────────────────────────────────────────

/** A flushed chunk of streamed text. */
export interface TextChunk {
  type: "text_chunk";
  ctx: BlockContext;
  text: string;
}

/** Complete text from a text-streaming section. */
export interface TextDone {
  type: "text_done";
  ctx: BlockContext;
  fullText: string;
  hasCodeBlocks: boolean;
  /** True when this persisted assistant text came from an interrupted turn. */
  interrupted?: boolean;
}

// ── Reasoning ────────────────────────────────────────────

/** Reasoning has started — show a thinking indicator. */
export interface ReasoningStartBlock {
  type: "reasoning_start";
  ctx: BlockContext;
}

/**
 * An incremental reasoning chunk — analog of `TextChunk`.
 *
 * Emitted while reasoning is still in progress so renderers can
 * show live progress (e.g. Codex's command/reasoning stream during
 * the long tool-call window). The eventual `ReasoningBlock` is
 * suppressed when any chunks were emitted, to avoid the formatter
 * re-rendering the same text as a summary panel.
 */
export interface ReasoningChunk {
  type: "reasoning_chunk";
  ctx: BlockContext;
  text: string;
}

/**
 * A completed reasoning/thinking block.
 *
 * Emitted only when no `ReasoningChunk` was streamed for this
 * reasoning section. Carries the full accumulated reasoning so
 * non-streaming renderers (logs, web UIs that prefer cards) still
 * get a single summary block.
 */
export interface ReasoningBlock {
  type: "reasoning_block";
  ctx: BlockContext;
  reasoningText: string;
  summaryText: string;
}

// ── Status ───────────────────────────────────────────────

/**
 * An error during the response.
 *
 * `message` may be empty when the server emitted `response.error`
 * without populating it — renderers should fall back to `code` so the
 * user sees at least the error classification instead of a blank
 * panel.
 */
export interface ErrorBlock {
  type: "error";
  ctx: BlockContext;
  message: string;
  /** Where the error originated, e.g. "llm". */
  source: string;
  /** Machine-readable error code, e.g. "llm_auth_failed". Empty when omitted. */
  code: string;
}

/** The server is retrying. */
export interface RetryBlock {
  type: "retry";
  ctx: BlockContext;
  /** What is being retried, e.g. "tool". */
  source: string;
  attempt: number;
  maxAttempts: number;
  delaySeconds: number;
}

/**
 * Compaction is in flight — the LLM summarization has started but
 * not yet finished. Emitted from `response.compaction.in_progress`.
 * Replaced in the render layer by `CompactionBlock` once
 * `response.compaction.completed` arrives.
 */
export interface CompactionInProgressBlock {
  type: "compaction_loading";
  ctx: BlockContext;
}

/** Conversation compaction finished. Emitted from `response.compaction.completed`. */
export interface CompactionBlock {
  type: "compaction";
  ctx: BlockContext;
}

/** A file artifact produced by the agent. */
export interface FileBlock {
  type: "file";
  ctx: BlockContext;
  fileId: string;
  filename: string | null;
}

/** A policy denied the user's input or the LLM call. */
export interface PolicyDeniedBlock {
  type: "policy_denied";
  ctx: BlockContext;
  /** Human-readable reason from the policy engine. */
  reason: string;
  /** Which phase fired: "request", "llm_request", etc. */
  phase: string;
}

/** The response reached a terminal state. */
export interface ResponseEndBlock {
  type: "response_end";
  ctx: BlockContext;
  /** Terminal status, e.g. "completed" or "failed". */
  status: string;
  /** The full response object, or `null`. */
  response: Response | null;
}

/**
 * An MCP-shape elicitation request, surfaced as an inline approval
 * prompt. Emitted from `response.elicitation_request` SSE events
 * (policy ASK round-trips). The user accepts or rejects via the
 * approval card; the store calls `approve()` on
 * `POST /v1/sessions/{id}/events` to resolve the parked tool.
 *
 * The `status` field is locally mutable — flips to `"responded"`
 * (with `response` populated) when the user submits. There is no
 * server-emitted "elicitation resolved" event; the agent simply
 * resumes (or refuses) and emits whatever the policy outcome
 * dictates as the next stream events.
 */
export interface ElicitationBlock {
  type: "elicitation";
  ctx: BlockContext;
  elicitationId: string;
  /**
   * Session whose resolve endpoint should receive the verdict.
   * Present when a child/sub-agent prompt is mirrored into an
   * ancestor chat; null/undefined means use the active session.
   */
  targetSessionId?: string | null;
  /** Human-readable message describing what's being gated. */
  message: string;
  /** "input" | "tool_call" | "tool_result" | "output". */
  phase: string;
  /** Name of the deciding policy (informational). */
  policyName: string;
  /** Truncated snapshot of the gated content. */
  contentPreview: string;
  /** A restricted-JSON-Schema form. `{}` for a binary accept/decline. */
  requestedSchema: Record<string, unknown>;
  /** Standalone approval page URL when ``mode === "url"``. */
  url?: string | null;
  /**
   * Local UI state — `"responded"` after the user submits via this
   * card OR after the chat store auto-resolves it (the matching
   * function call's `output_item.done` arrived while still pending,
   * meaning Claude proceeded via the TUI prompt). The auto-resolve
   * path sets `response.action` to `"auto_resolved"` so the card
   * renders a neutral "Resolved elsewhere" pill rather than the
   * Approved/Rejected pair that would imply a verdict the UI never
   * actually received.
   */
  status: "pending" | "responded";
  /**
   * What the user submitted, or `null` while still pending.
   * ``content`` is the optional MCP ``ElicitResult.content`` —
   * populated for multi-choice elicitations (AskUserQuestion) so
   * the responded card can render "Selected: <label>" rather than
   * a generic "Approved" pill, and for the "Accept & allow all
   * edits" action (`{allow_all_edits: true}`) so the responded card
   * can show that the session switched to auto-accept-edits mode.
   */
  response: {
    action: "accept" | "decline" | "cancel" | "auto_resolved";
    content?: Record<string, unknown>;
  } | null;
  /**
   * Structured AskUserQuestion payload — present when the gated
   * tool is Claude's built-in AskUserQuestion. ApprovalCard reads
   * this in preference to parsing the (truncated)
   * ``contentPreview`` JSON. Optional/null for every other
   * elicitation.
   */
  askUserQuestion?: Record<string, unknown> | null;
  /**
   * Full ExitPlanMode tool_input (untruncated) — present when the
   * gated tool is Claude's built-in ExitPlanMode. ApprovalCard
   * renders the ``plan`` markdown as a plan-review card.
   * Optional/null for every other elicitation.
   */
  exitPlanMode?: Record<string, unknown> | null;
  /**
   * Structured Codex command-approval payload. Present for
   * codex-native command approvals so the card can show command,
   * working directory, and reason without exposing transport ids.
   */
  codexCommand?: {
    command: string;
    cwd: string | null;
    reason: string | null;
    execPolicyAmendment: string[] | null;
  } | null;
  /**
   * Claude-native edit-tool prompts only: when true, the card renders
   * an "Accept & allow all edits" button that switches the session
   * into Claude Code's ``acceptEdits`` mode on accept (the web
   * equivalent of the native shift+tab toggle). Absent/false for all
   * other elicitations, so the button never appears where the mode
   * switch is a no-op.
   */
  allowAllEdits?: boolean;
  /**
   * Claude-native non-edit tool prompts only: present when the card
   * should render an "Approve & don't ask again for <host|tool>" button
   * that installs a session-scoped allow rule on accept (the web
   * equivalent of the native TUI's "don't ask again" option). ``tool``
   * is the gated tool; ``host`` is the WebFetch domain when present.
   * Absent/null for all other elicitations.
   */
  rememberScope?: RememberScope | null;
}

/** Union of all block types. */
export type AnyBlock =
  | ResponseStartBlock
  | UserMessageBlock
  | ToolGroup
  | ToolResultBlock
  | NativeToolBlock
  | SlashCommandBlock
  | RoutingDecisionBlock
  | TerminalCommandBlock
  | TextChunk
  | TextDone
  | ReasoningStartBlock
  | ReasoningChunk
  | ReasoningBlock
  | ErrorBlock
  | RetryBlock
  | CompactionInProgressBlock
  | CompactionBlock
  | FileBlock
  | ElicitationBlock
  | PolicyDeniedBlock
  | ResponseEndBlock;
