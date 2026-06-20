// Mirrors the response/error/usage types in
// sdks/python-client/omnigent_client/_types.py.
//
// Hand-ported, minimal subset — only the surface that `blocks.ts` and
// `events.ts` reference. The Python module also defines `Agent`,
// `File`, `Conversation`, and `PaginatedList`; those belong with the
// HTTP client work in Phase 1+, not the reducer port.
//
// Session types (`Session`, `SessionEventInput`, `SessionStatus`)
// mirror `omnigent/server/schemas.py` (`SessionResponse`,
// `SessionEventInput`, `SessionStatusEvent.status`).

import type { ConversationItem } from "./conversationItems";
import type { MessageContentBlock } from "./blocks";

/** Reference to a conversation, as returned on response objects. */
export interface ConversationRef {
  id: string;
}

/**
 * An un-consumed web-composer user message replayed from the session
 * snapshot. Native-terminal sessions don't persist a web message at
 * POST time (the transcript forwarder is the single writer), so the
 * server holds these in-memory and replays them so a client that
 * posted then navigated away / rebound re-hydrates the optimistic
 * bubble. Drained (and dropped by id via `clearedPendingId`) once the
 * message round-trips back through the transcript.
 */
export interface PendingInput {
  /** Server index id, e.g. ``"pending_a1b2c3"``; the bubble's stable key. */
  pendingId: string;
  /** Message content blocks as POSTed (file blocks carry real ids). */
  content: MessageContentBlock[];
  /** Authenticated identity of the posting actor, e.g. ``"alice@example.com"``. Absent when unknown. */
  createdBy?: string;
}

/** Token usage statistics for a completed response. */
export interface Usage {
  inputTokens: number;
  outputTokens: number;
  /** ``null`` when the provider did not report a total (legacy responses). */
  totalTokens: number | null;
  /**
   * Context-fill estimate for the next turn — set only by executors that make
   * multiple LLM sub-calls per turn (e.g. ``openai-agents``). ``null`` for
   * single-call turns and legacy responses. The REPL and web context ring
   * should prefer this over ``totalTokens`` when present.
   */
  contextTokens: number | null;
}

/** Structured error information from the server. */
export interface ErrorInfo {
  code: string;
  message: string;
}

/** Details about why a response stopped early. */
export interface IncompleteDetails {
  /** "max_iterations", "execution_timeout", "context_overflow", etc. */
  reason: string;
}

/**
 * A response object from the server.
 *
 * Mirrors the JSON shape carried inside `response.*` SSE events on
 * the `/v1/sessions/{id}/stream` channel (e.g. `response.created`,
 * `response.completed`). Post-`/v1/sessions` migration, `Response`
 * is no longer a top-level resource the UI fetches — it is the
 * inner payload of in-stream response lifecycle events, used for
 * intra-session task boundaries and bubble grouping.
 *
 * Only `id`, `status`, and `model` are required; the rest are
 * optional so test fixtures can build minimal responses without
 * filling every field.
 */
export interface Response {
  id: string;
  /** "queued" | "in_progress" | "completed" | "failed" | "incomplete" | "cancelled". */
  status: string;
  model: string;
  output?: Array<Record<string, unknown>>;
  createdAt?: number;
  completedAt?: number | null;
  previousResponseId?: string | null;
  conversation?: ConversationRef | null;
  usage?: Usage | null;
  error?: ErrorInfo | null;
  incompleteDetails?: IncompleteDetails | null;
  background?: boolean;
  instructions?: string | null;
}

// ── Content blocks ───────────────────────────────────────

/**
 * A typed content block for user messages.
 *
 * Used when sending messages via `POST /v1/sessions/{id}/events`.
 * Mirrors the `content` array that `content_resolver.py` and the
 * sessions schema accept on the server side.
 */
export type ContentBlock =
  | { type: "input_text"; text: string }
  | { type: "input_image"; file_id: string; filename?: string }
  | { type: "input_file"; file_id: string; filename: string };

// ── Sessions (/v1/sessions) ──────────────────────────────

/**
 * Session lifecycle status.
 *
 * `idle | running | failed` arrives on the snapshot
 * (`GET /v1/sessions/{id}`). `waiting` is added by
 * `session.status` SSE events only — it surfaces while the parent
 * agent loop is parked on the async-work drain (background tools /
 * sub-agents). Snapshot rehydration cannot yield `waiting`; live
 * events can.
 *
 * Mirrors the union in `SessionStatusEvent.status`. See
 * `omnigent/server/schemas.py:SessionStatusEvent`.
 */
export type SessionStatus = "idle" | "launching" | "running" | "waiting" | "failed";

/**
 * A client-submitted event/input for a session.
 *
 * Body shape for `POST /v1/sessions/{id}/events`. The `type`
 * discriminator routes the route-layer interpretation of `data`:
 *
 * - `"message"`: `{ role: "user", content: ContentBlock[] }`
 * - `"function_call_output"`: `{ call_id, output, ... }`
 * - `"approval"`: `{ elicitation_id, ...ElicitResult }`
 * - `"interrupt"`: `{}` (empty data)
 * - `"stop_session"`: `{}` (empty data) — terminate the live session
 *   without deleting its conversation (owner-only)
 * - `"slash_command"`: `{ kind: "skill", name, arguments }` — invoke a
 *   skill the same way the REPL does. The server resolves the skill,
 *   persists a visible receipt plus a hidden `<skill>` meta message,
 *   and forwards the meta to the runner (see
 *   `_dispatch_skill_slash_command_to_runner`).
 *
 * Mirrors `omnigent.server.schemas.SessionEventInput`.
 */
export type SessionEventInput =
  | { type: "message"; data: { role: "user"; content: ContentBlock[] } }
  | { type: "function_call_output"; data: Record<string, unknown> }
  | { type: "approval"; data: Record<string, unknown> }
  | { type: "interrupt"; data?: Record<string, unknown> }
  | { type: "stop_session"; data?: Record<string, unknown> }
  | { type: "slash_command"; data: { kind: "skill"; name: string; arguments: string } }
  | { type: string; data: Record<string, unknown> };

/**
 * A session snapshot item.
 *
 * `GET /v1/sessions/{id}` returns `items: ConversationItem[]` in
 * `SessionResponse`. The shape on the wire is not yet pinned
 * against the flat shape `GET /v1/conversations/{id}/items` uses
 * (see migration plan R16) — Pydantic serialization may produce a
 * nested `{ id, type, response_id, status, data }` envelope. We
 * type the union and defer normalization until PR 5 wires
 * snapshot hydration.
 */
export type SessionItem = ConversationItem | NestedSessionItem;

/**
 * Nested item envelope variant emitted by the sessions snapshot
 * route when items are serialized via Pydantic without the
 * conversations route's `to_api_dict` flattening.
 */
export interface NestedSessionItem {
  id: string;
  type: string;
  response_id: string;
  status: string;
  created_at?: number;
  data: Record<string, unknown>;
}

/**
 * Snapshot of a session as returned by `POST /v1/sessions` and
 * `GET /v1/sessions/{id}`.
 *
 * `id` equals the underlying conversation id (`conv_*`). Field
 * naming follows TS conventions (camelCase); the wire uses
 * snake_case (`agent_id`, `created_at`) so callers reading the
 * snapshot must map at the boundary.
 *
 * `queuedItems` is documented in `omnigent/server/API.md` but is
 * NOT present on `SessionResponse` in `schemas.py` today (see
 * migration plan R5). Treated as optional here; absent until the
 * server schema is aligned.
 *
 * Mirrors `omnigent.server.schemas.SessionResponse`.
 */
/**
 * Cumulative token/cost usage attributed to a single LLM model — one value
 * in `Session.usageByModel`. Counts are summed over the session subtree.
 * Each field is `null` when that bucket was not recorded for the model;
 * `totalCostUsd` is `null` when the model's turns were unpriced. Mirrors
 * `omnigent.server.schemas.ModelUsage`.
 */
export interface ModelUsage {
  inputTokens: number | null;
  outputTokens: number | null;
  totalTokens: number | null;
  cacheReadInputTokens: number | null;
  cacheCreationInputTokens: number | null;
  totalCostUsd: number | null;
}

export interface Session {
  id: string;
  agentId: string;
  /**
   * Human-readable name of the bound agent, e.g. ``"research-agent"``.
   * Populated from ``SessionResponse.agent_name`` on the wire. ``null``
   * when the agent row cannot be found (deleted or orphaned session).
   */
  agentName: string | null;
  runnerId?: string | null;
  /**
   * Host that launched (or should launch) the runner, e.g.
   * ``"host_a1b2"``; ``null`` for CLI/local sessions. Carried on the
   * snapshot so a directly-opened (off-sidebar / child) session — for
   * which the sidebar row (`Conversation`) is absent — can still derive
   * its open-session liveness (host-bound vs. local), and so the
   * reconnect/fork affordance picks the right path (the fork-resume
   * picker also defaults to the source session's host). Optional like
   * the other snapshot-derived fields: the server always sends it, but
   * older recorded fixtures may omit it (treated as `null`).
   */
  hostId?: string | null;
  status: SessionStatus;
  createdAt: number;
  /**
   * Human-readable session title, e.g. ``"researcher:auth"`` for a
   * sub-agent (the spawn tool seeds this) or a user-supplied string
   * for a top-level session. ``null`` when unset.
   */
  title: string | null;
  /**
   * Session-scoped guardrails labels (includes `omnigent.wrapper` /
   * `omnigent.ui` markers the picker reads, and
   * `omnigent.fork.source_id` on an unbound coding clone).
   */
  labels?: Record<string, string>;
  /**
   * Canonical working directory the runner starts in, e.g.
   * ``"/Users/alice/myrepo"`` (or a worktree path). ``null`` when
   * unbound. The fork-resume picker prefills the source's value.
   */
  workspace?: string | null;
  /**
   * Git branch checked out in a server-created worktree, e.g.
   * ``"feature/login"``. ``null`` when the session uses no worktree.
   * When set on the source, the picker offers a worktree/branch
   * selector for the clone.
   */
  gitBranch?: string | null;
  items: SessionItem[];
  queuedItems?: SessionEventInput[];
  /** Per-session reasoning-effort override, e.g. ``"high"``. */
  reasoningEffort?: string | null;
  /** LLM model identifier from the bound agent's spec. */
  llmModel?: string | null;
  /**
   * Effective brain harness for the session, e.g. ``"claude-sdk"`` or
   * ``"pi"``. Reflects a create-time ``harness_override`` when one was
   * picked, else the agent spec's declared harness. Drives the chat
   * composer's "Polly (Pi)" pill suffix.
   */
  harness?: string | null;
  /**
   * Per-session LLM model override, e.g. ``"claude-opus-4-7"``. Set
   * via the picker or REPL's ``/model`` — both write the same column
   * so the surfaces stay in sync.
   */
  modelOverride?: string | null;
  /**
   * Per-session cost-control switch: `"on"` activates the spec's
   * configured cost-control mode, `"off"` disables cost control for
   * this session, `null` defers to the spec default. Driven by the
   * "Cost Optimized" toggle.
   */
  costControlModeOverride?: "on" | "off" | null;
  /** Model context window size in tokens as looked up server-side. */
  contextWindow?: number | null;
  /**
   * Input token count from the most recently completed task's usage.
   * ``null`` when no task has completed yet. Lets the context-ring
   * render immediately on conversation resume.
   */
  lastTotalTokens?: number | null;
  /**
   * Cumulative session spend in USD, server-computed (the cost-budget
   * total). ``null``/absent when the session is **unpriced** (no turn
   * priced yet), so the UI renders "—" rather than ``$0.00``. Lets the
   * cost indicator render immediately on conversation resume.
   */
  totalCostUsd?: number | null;
  /**
   * Per-model breakdown of the same subtree usage, keyed by the raw harness
   * model id (e.g. `"claude-sonnet-4-6"`). Each value is the model's token
   * buckets + optional USD cost. `null`/absent when no per-model usage has
   * been recorded. Lets the popover show which models a session spent on.
   */
  usageByModel?: Record<string, ModelUsage> | null;
  /**
   * Error details from the most recently failed task. Only present
   * when ``status === "failed"`` and the task stored an error.
   * Lets clients display the failure reason on historical load without
   * relying on the transient ``response.error`` SSE event, which may
   * have been emitted before the web client subscribed.
   */
  lastTaskError?: { code: string; message: string } | null;
  /**
   * Outstanding `response.elicitation_request` event payloads on
   * the snapshot. Replayed into the chat as ApprovalCard blocks on
   * cold load because the SSE stream itself has no replay buffer.
   * Empty when no prompts are pending. Each entry mirrors the
   * raw SSE shape so the existing `sse.ts` parser can fold them
   * back into the block stream.
   */
  pendingElicitations?: Array<Record<string, unknown>>;
  /**
   * Un-consumed web-composer user messages on native-terminal
   * sessions at snapshot time. Replayed so a client that posted then
   * navigated away / rebound re-hydrates the optimistic bubble. Empty
   * for non-native sessions (their message is already in `items`).
   */
  pendingInputs?: PendingInput[];
  /**
   * Requesting user's numeric permission level on this session
   * (1=read, 2=edit, 3=manage, 4=owner). ``null`` when permissions
   * are disabled or the user has no grant. For sub-agent (child)
   * sessions the server's read-side gate (``check_session_access``)
   * already delegates to the parent, but this field today reflects
   * direct grants only — child sessions land here as ``null`` for
   * non-admin users with parent-only access. The UI treats ``null``
   * permissively, so that's fine for unblocking interaction.
   */
  permissionLevel: number | null;
  /**
   * Parent conversation id when this session is a sub-agent (child),
   * e.g. ``"conv_parent987"``. ``null`` for top-level sessions.
   * Used by ``AppShell`` to compute the Subagents tab's
   * ``rootSessionId`` (the rail lists the parent's children + a
   * "main" link back to the parent, from either the parent or any
   * sibling) and to render the header's "Back" button + sub-agent
   * identity label when the user is inside a child.
   */
  parentSessionId: string | null;
  /**
   * For sub-agent (child) sessions, the sub-agent type name within
   * the parent's spec tree, e.g. ``"claude_code"``. ``null`` for
   * top-level sessions. Preferred over ``agentName`` when labeling
   * a child session's surfaces (e.g. the embedded REPL terminal
   * tab), since ``agentName`` carries the parent bundle's name.
   */
  subAgentName: string | null;
  /**
   * Current Claude Code todo list for `omnigent claude` sessions.
   * Sourced from the server's `_session_todos_cache` at snapshot
   * build time so the panel survives page refresh. Empty array for
   * non-claude-native sessions or before the first turn creates todos.
   */
  todos?: Array<{
    content: string;
    status: "pending" | "in_progress" | "completed";
    activeForm: string;
  }>;
  /**
   * Skills the bound agent has access to (bundled + host-discovered,
   * subject to the spec's ``skills_filter``). Populated by the
   * server from the agent cache; ``undefined`` on older snapshots.
   * The web composer surfaces these in its slash-command menu so
   * users can fire ``/skill-name``.
   */
  skills?: SkillSummary[];
  /**
   * Codex app-server model options for codex-native sessions. Each option
   * comes from Codex ``model/list`` and carries the model-specific reasoning
   * efforts the picker should offer. Empty for non-codex-native sessions or
   * while the runner has not answered yet.
   */
  codexModelOptions?: CodexModelOption[];
  /**
   * True while the runner is auto-creating the terminal for a
   * terminal-first session (claude-native / codex-native). Sourced
   * from the server's `_session_terminal_pending_cache` at snapshot
   * build time so a client connecting mid-spin-up sees the Terminal
   * pill spinner. ``undefined`` on older snapshots (treated as false).
   */
  terminalPending?: boolean;
  /**
   * Managed-sandbox launch progress. Present while the session's
   * background sandbox launch is in flight or has failed; `null` for
   * sessions without a managed launch and once the launch succeeds.
   * Sourced from the server's `_session_sandbox_status_cache` at
   * snapshot build time so a client opening the session mid-launch
   * sees the current stage.
   */
  sandboxStatus?: SandboxStatus | null;
}

/**
 * Stages of a managed-sandbox launch, in pipeline order. `cloning`
 * only occurs when the session requested a repository workspace;
 * `ready` and `failed` are terminal (`ready` is delivered via SSE
 * only — the snapshot field clears to null on success).
 */
export type SandboxLaunchStage =
  | "provisioning"
  | "cloning"
  | "starting"
  | "connecting"
  | "ready"
  | "failed";

/**
 * Managed-sandbox launch progress — mirrors
 * `omnigent.server.schemas.SandboxStatus`. Drives the provisioning
 * indicator on the session page while the sandbox launches in the
 * background.
 */
export interface SandboxStatus {
  /** Current launch stage, e.g. `"provisioning"`. */
  stage: SandboxLaunchStage;
  /** Failure detail when `stage === "failed"`; `null` otherwise. */
  error?: string | null;
}

/**
 * One entry in ``Session.skills`` — mirrors
 * ``omnigent.server.schemas.SkillSummary``. Just the name +
 * one-line description so the composer's suggestion menu can list
 * them; the full skill body is loaded server-side at invocation
 * time.
 */
export interface SkillSummary {
  /** Lowercase kebab-case identifier, e.g. ``"triage-issues"``. */
  name: string;
  /** One-line summary from the SKILL.md frontmatter. */
  description: string;
}

/**
 * One raw Codex model option from app-server ``model/list``.
 */
export interface CodexReasoningEffortOption {
  /** Codex effort id, e.g. ``"xhigh"``. */
  reasoningEffort: string;
  /** Codex-provided description, when present. */
  description?: string;
  /** Additional Codex metadata. */
  [key: string]: unknown;
}

export interface CodexModelOption {
  /** Codex picker id to pass back to ``thread/settings/update``. */
  id: string;
  /** Provider-facing model id Codex will run. */
  model?: string;
  /** Codex display label. */
  displayName?: string;
  /** Default reasoning effort for this model. */
  defaultReasoningEffort?: string;
  /** Raw effort objects Codex advertises for this model. */
  supportedReasoningEfforts?: CodexReasoningEffortOption[];
  /** Whether Codex marks this as the default model. */
  isDefault?: boolean;
  /** Additional Codex metadata. */
  [key: string]: unknown;
}
