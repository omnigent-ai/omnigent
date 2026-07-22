// Typed client for the four `/v1/sessions` endpoints introduced in
// commit `e64a490` ("Migrate session ↔ client interactions to
// /v1/sessions"). Mirrors `omnigent/server/routes/sessions.py`.
//
// All requests go through the existing Vite `/v1` proxy
// (`web/vite.config.ts`) so no proxy changes are needed when this
// module starts being used.
//
// Naming: TS surface is camelCase; the wire is snake_case. The
// helpers below convert at the boundary so callers never see raw
// wire fields.

import type { QueryClient } from "@tanstack/react-query";
import type { ConversationItem } from "./conversationItems";
import { isMessageItem } from "./conversationItems";
import type { MessageContentBlock } from "./blocks";
import type { McpServerStartup } from "./events";
import { authenticatedFetch } from "./identity";
import { isAndroidShell, isElectronShell, isIOSShell } from "@/lib/nativeBridge";
import type {
  CodexModelOption,
  ModelUsage,
  NestedSessionItem,
  SandboxStatus,
  Session,
  SessionEventInput,
  SessionItem,
  SessionStatus,
  SkillSummary,
} from "./types";

/** Returns the client surface label for the X-Omnigent-Client telemetry header. */
function getClientSurface(): string {
  if (isElectronShell()) return "desktop";
  if (isIOSShell()) return "ios";
  if (isAndroidShell()) return "android";
  return "web";
}

/**
 * MCP-shape elicitation response, used as the `result` argument to
 * `approve`. Mirrors MCP's `ElicitResult` (`action` + optional
 * `content`). The route layer validates the merged payload in
 * `_dispatch_approval`.
 */
export interface ElicitResult {
  action: "accept" | "decline" | "cancel";
  content?: Record<string, unknown>;
}

/** Response body of `POST /v1/sessions/{id}/events` (202 Accepted). */
export interface PostEventResponse {
  /** True for item-typed events (persisted); false for interrupt / approval. */
  queued: boolean;
  /**
   * Store-assigned conversation item id for item-typed events.
   * Matches `session.input.consumed.data.item_id`; absent for
   * interrupt / approval events, which are not persisted as items.
   */
  itemId?: string;
  /** True when a policy denied the input. The server published the
   *  denial text via SSE but did not start a turn or persist the
   *  user message, so no `session.input.consumed` will follow. */
  denied?: boolean;
  /**
   * Server-assigned pending-input id for a native-terminal web
   * message, e.g. ``"pending_a1b2c3"``. It identifies the snapshot's
   * replayed pending-input bubble on rebind and is the
   * `clearedPendingId` the consume event carries to drop it. A client
   * *may* adopt it onto its live optimistic bubble for id-based dedupe;
   * this store deliberately does NOT (it keeps the client temp id for
   * React-key stability and relies on `stableKey` + FIFO matching), so
   * adoption is optional. Absent for non-native messages and non-message
   * events.
   */
  pendingId?: string;
}

/**
 * Wire shape of `ModelUsage` from `omnigent/server/schemas.py` — one
 * per-model entry in `usage_by_model`. Snake-case; converted to the
 * camelCase `ModelUsage` type at the parse boundary. Every field is
 * optional (absent when that bucket was not recorded for the model).
 */
interface ModelUsageWire {
  input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
  cache_read_input_tokens?: number | null;
  cache_creation_input_tokens?: number | null;
  total_cost_usd?: number | null;
}

/**
 * Wire shape of `SessionResponse` from
 * `omnigent/server/schemas.py`. Snake-case; converted to the
 * camelCase `Session` type at the parse boundary.
 */
interface SessionResponseWire {
  id: string;
  agent_id: string;
  /** Human-readable name of the bound agent, e.g. ``"research-agent"``. */
  agent_name?: string | null;
  runner_id?: string | null;
  /**
   * Host that launched (or should launch) the runner, e.g. ``"host_a1b2"``.
   * ``null`` for CLI/local sessions with no host. Needed so a
   * directly-opened (off-sidebar / child) session can still derive its
   * open-session liveness — the sidebar row (`Conversation`) is the only
   * other carrier and it's absent for those.
   */
  host_id?: string | null;
  /**
   * Whether this session is bound to a dormant managed host the server can
   * wake in place (its sandbox provider supports resume). Read only when the
   * host is offline, to tell a recoverable "asleep" state (send a message —
   * the server resumes the sandbox) from the terminal host_offline dead-end.
   * Absent/`false` for non-managed/non-resumable hosts.
   */
  host_resumable?: boolean;
  status: SessionStatus;
  /**
   * Background shells (claude-native) still running as of the last status
   * edge, so a reload re-shows "N background tasks still running" after the session
   * has settled to ``"idle"``. Absent/0 when none are tracked.
   */
  background_task_count?: number | null;
  created_at: number;
  /**
   * Human-readable session title, e.g. ``"researcher:auth"`` for a
   * sub-agent or a user-supplied string for a top-level session.
   * Optional on the wire (the server omits it when unset).
   */
  title?: string | null;
  labels?: Record<string, string>;
  /** Canonical working directory; ``null`` when unbound. */
  workspace?: string | null;
  /** Worktree branch; ``null`` when the session uses no worktree. */
  git_branch?: string | null;
  items?: SessionItem[];
  // `queued_items` is documented in `omnigent/server/API.md` but
  // is not on `SessionResponse` today (migration plan R5). Typed
  // optional so we read it forward-compatibly when added.
  queued_items?: SessionEventInput[];
  reasoning_effort?: string | null;
  llm_model?: string | null;
  /** Effective brain harness (override-aware), e.g. ``"claude-sdk"``. */
  harness?: string | null;
  model_override?: string | null;
  /** Per-session cost-control switch; `null`/absent = spec default. */
  cost_control_mode_override?: "on" | "off" | null;
  context_window?: number | null;
  last_total_tokens?: number | null;
  total_cost_usd?: number | null;
  /**
   * Per-model breakdown of the same subtree usage, keyed by the raw harness
   * model id. Each value is a `ModelUsage` (the five token buckets + optional
   * `total_cost_usd`). Absent/`null` when no per-model usage was recorded.
   */
  usage_by_model?: Record<string, ModelUsageWire> | null;
  last_task_error?: { code: string; message: string } | null;
  /**
   * Outstanding `response.elicitation_request` event dicts at the
   * moment the snapshot was built. The live SSE stream has no
   * replay, so a prompt emitted before the user opened the chat
   * would otherwise never render. Each entry is shaped like the
   * SSE event the chat would have received live — same fields the
   * `sse.ts` parser already handles.
   */
  pending_elicitations?: Array<Record<string, unknown>>;
  /**
   * Un-consumed web-composer user messages on native-terminal sessions
   * at snapshot time, each ``{pending_id, content}``. Replayed so a
   * client that posted then navigated away / rebound re-hydrates the
   * optimistic bubble. Empty for non-native sessions.
   */
  pending_inputs?: Array<{
    pending_id: string;
    content: MessageContentBlock[];
    created_by?: string;
  }>;
  /**
   * Numeric permission level (1=read, 2=edit, 3=manage, 4=owner) the
   * authenticated user holds on this session. Optional on the wire
   * because the server omits it when permissions are disabled
   * entirely, and absent on older recorded fixtures.
   */
  permission_level?: number | null;
  /**
   * Parent conversation id when this session is a sub-agent (child),
   * e.g. ``"conv_parent987"``. ``null`` (or absent on older fixtures)
   * for top-level sessions. Lets the UI distinguish a child from a
   * regular session without an extra request.
   */
  parent_session_id?: string | null;
  /**
   * For sub-agent (child) sessions, the sub-agent type name within
   * the parent's spec tree, e.g. ``"claude_code"``. ``null`` (or
   * absent) for top-level sessions.
   */
  sub_agent_name?: string | null;
  todos?: Array<{
    content: string;
    status: "pending" | "in_progress" | "completed";
    activeForm: string;
  }>;
  /**
   * Skills the bound agent can invoke — bundled + host-discovered
   * (subject to the spec's ``skills_filter``). Just name + one-line
   * description. Surfaced in the web composer's slash-command menu.
   */
  skills?: SkillSummary[];
  model_options?: CodexModelOption[];
  /**
   * True while the runner is auto-creating a terminal-first session's
   * terminal. Drives the Terminal-pill spinner; absent on older
   * snapshots (treated as false).
   */
  terminal_pending?: boolean;
  /**
   * Managed-sandbox launch progress while the background launch is in
   * flight or has failed; absent/null otherwise. Mirrors
   * `omnigent.server.schemas.SandboxStatus`.
   */
  sandbox_status?: SandboxStatus | null;
  mcp_startup?: Record<string, McpServerStartup> | null;
  /**
   * Response id of the turn currently in flight, or absent/null when
   * idle. Lets a client reconnecting mid-turn reopen a streaming
   * `activeResponse` (the turn-start `running` SSE edge is not replayed).
   */
  active_response_id?: string | null;
}

interface SessionItemsResponseWire {
  object: "list";
  data: ConversationItem[];
  first_id: string | null;
  last_id: string | null;
  has_more: boolean;
}

/**
 * Initial (and per-scroll-up-page) item count for conversation history
 * hydration. Bounds how many items the chat surface fetches, parses,
 * and renders when a conversation is opened. The synchronous first-paint
 * render scales ~linearly with this count (parse/walk are negligible;
 * mounting the markdown/Streamdown tree per bubble is the cost), so a
 * smaller window keeps thread-switch render cheap. `loadMoreHistory`
 * pages older items on scroll-up.
 */
export const SESSION_HISTORY_PAGE_SIZE = 20;

/**
 * Convert the snake-case `usage_by_model` wire map into the camelCase
 * `Record<string, ModelUsage>` the store/UI use, or `null` when absent.
 * A `null`/absent value (or `null` token bucket) maps to `null` on the
 * camelCase side so the UI omits that row.
 */
function usageByModelFromWire(
  wire: Record<string, ModelUsageWire> | null | undefined,
): Record<string, ModelUsage> | null {
  if (wire == null) return null;
  const out: Record<string, ModelUsage> = {};
  for (const [model, usage] of Object.entries(wire)) {
    out[model] = {
      inputTokens: usage.input_tokens ?? null,
      outputTokens: usage.output_tokens ?? null,
      totalTokens: usage.total_tokens ?? null,
      cacheReadInputTokens: usage.cache_read_input_tokens ?? null,
      cacheCreationInputTokens: usage.cache_creation_input_tokens ?? null,
      totalCostUsd: usage.total_cost_usd ?? null,
    };
  }
  return out;
}

function sessionFromWire(wire: SessionResponseWire): Session {
  return {
    id: wire.id,
    agentId: wire.agent_id,
    agentName: wire.agent_name ?? null,
    runnerId: wire.runner_id,
    hostId: wire.host_id ?? null,
    hostResumable: wire.host_resumable ?? false,
    status: wire.status,
    backgroundTaskCount: wire.background_task_count ?? undefined,
    createdAt: wire.created_at,
    title: wire.title ?? null,
    labels: wire.labels,
    workspace: wire.workspace ?? null,
    gitBranch: wire.git_branch ?? null,
    items: wire.items ?? [],
    queuedItems: wire.queued_items,
    reasoningEffort: wire.reasoning_effort,
    llmModel: wire.llm_model,
    harness: wire.harness ?? null,
    modelOverride: wire.model_override,
    costControlModeOverride: wire.cost_control_mode_override,
    contextWindow: wire.context_window,
    lastTotalTokens: wire.last_total_tokens,
    totalCostUsd: wire.total_cost_usd,
    usageByModel: usageByModelFromWire(wire.usage_by_model),
    lastTaskError: wire.last_task_error,
    pendingElicitations: wire.pending_elicitations ?? [],
    pendingInputs: (wire.pending_inputs ?? []).map((p) => ({
      pendingId: p.pending_id,
      content: p.content,
      ...(p.created_by !== undefined ? { createdBy: p.created_by } : {}),
    })),
    permissionLevel: wire.permission_level ?? null,
    parentSessionId: wire.parent_session_id ?? null,
    subAgentName: wire.sub_agent_name ?? null,
    todos: wire.todos ?? [],
    skills: wire.skills ?? [],
    codexModelOptions: wire.model_options ?? [],
    terminalPending: wire.terminal_pending ?? false,
    sandboxStatus: wire.sandbox_status ?? null,
    mcpStartup: wire.mcp_startup ?? null,
    activeResponseId: wire.active_response_id ?? null,
  };
}

async function readJsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as T;
}

/**
 * An HTTP error from an AP route, carrying the server's machine-readable
 * `code` so callers can branch on the failure kind (e.g. show a friendly
 * message for ``runner_unavailable``) instead of string-matching the
 * status line.
 *
 * The server's :class:`OmnigentError` serializes as
 * ``{"error": {"code": "...", "message": "..."}}`` (see the FastAPI
 * exception handler in ``server/app.py``); `code` is `null` when the
 * body wasn't in that shape.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  constructor(message: string, status: number, code: string | null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

/**
 * Build an {@link ApiError} from a non-OK response, preferring the
 * server's `error.message` / `error.code` over the bare status line.
 * Falls back to ``"<status> <statusText>"`` when the body is missing or
 * not the AP error shape.
 */
async function apiErrorFromResponse(res: Response): Promise<ApiError> {
  let message = `${res.status} ${res.statusText}`;
  let code: string | null = null;
  try {
    const body = (await res.json()) as { error?: { code?: string; message?: string } };
    if (body.error?.message) message = body.error.message;
    if (body.error?.code) code = body.error.code;
  } catch {
    // Non-JSON / empty body — keep the status-line fallback.
  }
  return new ApiError(message, res.status, code);
}

function postEventResponseFromWire(wire: {
  queued: boolean;
  item_id?: string;
  denied?: boolean;
  pending_id?: string;
}): PostEventResponse {
  return {
    queued: wire.queued,
    itemId: wire.item_id,
    denied: wire.denied,
    pendingId: wire.pending_id,
  };
}

/**
 * Create a session bound to an agent. Identifies the agent by its
 * durable `agent_id`, NOT name (route requirement; the legacy
 * `/v1/responses` flow accepted the agent name as `model`).
 *
 * `initialItems` is supported on the wire but web should pass
 * `[]` and post the first message through `postEvent` after binding
 * the live stream — see migration plan R13. The `initialItems`
 * parameter is retained here for non-browser callers (CLI / tests)
 * that can tolerate snapshot-only catch-up.
 */
/**
 * Create a session bound to a registered agent via POST /v1/sessions.
 *
 * `options` covers the sub-agent / "Add agent" path: a child session is
 * created by passing `parentSessionId` (and optionally a `title`). Leave
 * `subAgentName` unset (or null) for a user-added agent so the runner
 * resolves the child's own bound agent_id rather than walking the
 * parent's declared sub-agent tree. Optional fields are only sent when
 * provided, so a plain `createSession(agentId)` posts the same minimal
 * body as before.
 *
 * @param agentId - Durable id of the agent to bind, e.g. "ag_abc123".
 * @param initialItems - Optional history seed (e.g. a first user message).
 * @param options.parentSessionId - Parent session id to attach this
 *   session under as a child, e.g. "conv_parent987".
 * @param options.subAgentName - Sub-agent name for parent-spec-tree
 *   resolution; null/omitted for user-added agents.
 * @param options.title - Child title, e.g. "ui:claude-native-ui:1".
 */
export async function createSession(
  agentId: string,
  initialItems: SessionEventInput[] = [],
  options: {
    parentSessionId?: string;
    subAgentName?: string | null;
    title?: string;
  } = {},
): Promise<Session> {
  const body: {
    agent_id: string;
    initial_items: SessionEventInput[];
    parent_session_id?: string;
    sub_agent_name?: string | null;
    title?: string;
  } = { agent_id: agentId, initial_items: initialItems };
  if (options.parentSessionId !== undefined) {
    body.parent_session_id = options.parentSessionId;
  }
  if (options.subAgentName !== undefined) {
    body.sub_agent_name = options.subAgentName;
  }
  if (options.title !== undefined) {
    body.title = options.title;
  }
  const res = await authenticatedFetch("/v1/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Omnigent-Client": getClientSurface() },
    body: JSON.stringify(body),
  });
  return sessionFromWire(await readJsonOrThrow<SessionResponseWire>(res));
}

/**
 * Create a session with an inline agent bundle via multipart
 * `POST /v1/sessions`.
 *
 * The server creates a session-scoped agent from the uploaded bundle
 * (a `.tar.gz` containing `config.yaml` and optionally `AGENTS.md`)
 * and binds it to the new session in one atomic operation. Session
 * metadata (host, workspace, labels, etc.) is sent as a JSON string
 * in the `metadata` form part.
 *
 * @param bundle - The agent bundle as a `File` (`.tar.gz`).
 * @param metadata - Session-level metadata (host_id, workspace, labels, etc.).
 * @returns The created session's id.
 */
export async function createBundledSession(
  bundle: File,
  metadata: {
    host_id?: string;
    host_type?: string;
    workspace?: string;
    labels?: Record<string, string>;
    terminal_launch_args?: string[];
    git?: { branch_name: string; base_branch?: string };
  } = {},
): Promise<{ id: string }> {
  const form = new FormData();
  form.append("metadata", JSON.stringify(metadata));
  form.append("bundle", bundle);
  const res = await authenticatedFetch("/v1/sessions", {
    method: "POST",
    headers: { "X-Omnigent-Client": getClientSurface() },
    body: form,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to create bundled session: ${res.status} ${text}`);
  }
  // The multipart response uses `session_id` (CreatedSessionResponse),
  // while the JSON path uses `id` (SessionResponse). Normalize to `id`
  // so callers don't need to care which path was taken.
  const body = (await res.json()) as { session_id: string };
  return { id: body.session_id };
}

/**
 * Fork (clone) a session into a new one via
 * POST /v1/sessions/{source_id}/fork.
 *
 * The server deep-copies the source's transcript and clones its agent
 * into a fresh, unbound session owned by the caller (read access on the
 * source is required). Comments and permissions are NOT copied, and the
 * fork starts `idle` with no runner — the caller binds their own via
 * `PATCH /v1/sessions/{id}`. `title` is only sent when provided; omitted,
 * the server derives `"Fork of <source title>"`.
 *
 * @param sourceId - Session to fork, e.g. "conv_abc123".
 * @param title - Optional title for the new fork.
 * @param agentId - Optional built-in agent to switch the fork to (e.g.
 *   fork a Claude-SDK session into Claude Code). Omitted → keep the
 *   source's agent. The server carries model settings (and native
 *   history) across only within the same provider family.
 * @param upToResponseId - Optional truncation point, e.g. "resp_abc". When
 *   set, the fork copies history only up to and including that response
 *   ("fork from here"); omitted, the full history is copied.
 */
export async function forkSession(
  sourceId: string,
  title?: string,
  agentId?: string,
  upToResponseId?: string,
): Promise<Session> {
  const body: { title?: string; agent_id?: string; up_to_response_id?: string } = {};
  if (title !== undefined) {
    body.title = title;
  }
  if (agentId !== undefined) {
    body.agent_id = agentId;
  }
  if (upToResponseId !== undefined) {
    body.up_to_response_id = upToResponseId;
  }
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sourceId)}/fork`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Omnigent-Client": getClientSurface() },
    body: JSON.stringify(body),
  });
  return sessionFromWire(await readJsonOrThrow<SessionResponseWire>(res));
}

/**
 * Switch an existing session in place to a different agent/harness:
 * ``POST /v1/sessions/{id}/switch-agent``.
 *
 * Unlike fork, this keeps the SAME session (transcript, comments, files,
 * workspace) and only rebinds the agent. The next turn runs on the new
 * harness; history carries per the same rule as a fork switch
 * (``forkTargetCarriesHistory``). Model settings reset to the target's
 * defaults on a cross-family switch. Only built-in agents are bindable,
 * and only while the session is idle (a running turn → 409).
 *
 * @param sessionId - The session to switch, e.g. ``"conv_abc123"``.
 * @param agentId - Built-in agent to switch to, e.g. ``"ag_builtin_codex"``.
 * @returns The session as it stands after the switch.
 * @throws Error carrying the server's failure detail (e.g. 409 when a turn
 *   is running) so the caller can surface it inline.
 */
export async function switchSessionAgent(sessionId: string, agentId: string): Promise<Session> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/switch-agent`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent_id: agentId }),
    },
  );
  return sessionFromWire(await readJsonOrThrow<SessionResponseWire>(res));
}

/**
 * Bind an existing (unbound) session to a host + working directory and
 * launch its runner: ``POST /v1/hosts/{hostId}/runners``.
 *
 * This is the fork-resume path — the clone already exists; this picks
 * its host/directory at resume time. When ``git`` is set the server
 * creates a worktree off ``workspace`` (the source repo) and binds the
 * runner to it; otherwise it binds ``workspace`` directly.
 *
 * @param hostId - Host the caller owns to launch on, e.g. ``"host_abc"``.
 * @param sessionId - The (unbound) session to bind, e.g. ``"conv_abc"``.
 * @param workspace - Absolute directory on the host. With ``git`` set,
 *   this is the source repository the worktree branches from.
 * @param git - Optional worktree options. ``baseBranch`` omitted → the
 *   host branches from the repo's current HEAD.
 * @returns The bound runner id.
 * @throws Error carrying the server's failure detail (e.g. a duplicate
 *   branch or an offline host) so the picker can surface it inline.
 */
export async function launchRunner(
  hostId: string,
  sessionId: string,
  workspace: string,
  git?: { branchName: string; baseBranch?: string; existingWorktree?: boolean },
): Promise<{ runnerId: string }> {
  const body: {
    session_id: string;
    workspace: string;
    git?: { branch_name: string; base_branch?: string; existing_worktree?: boolean };
  } = { session_id: sessionId, workspace };
  if (git !== undefined) {
    // `existing_worktree` binds a pre-existing worktree (no worktree is
    // created; the branch is recorded for the sidebar + delete flow), so it
    // never carries a base_branch.
    body.git = {
      branch_name: git.branchName,
      ...(git.existingWorktree
        ? { existing_worktree: true }
        : git.baseBranch !== undefined
          ? { base_branch: git.baseBranch }
          : {}),
    };
  }
  const res = await authenticatedFetch(`/v1/hosts/${encodeURIComponent(hostId)}/runners`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    // hosts.py raises HTTPException → ``{"detail": "..."}``. Surface the
    // server's reason (bad branch, offline host, already-bound) verbatim.
    let detail = `${res.status} ${res.statusText}`;
    try {
      const err = (await res.json()) as { detail?: string };
      if (typeof err.detail === "string" && err.detail) detail = err.detail;
    } catch {
      // Non-JSON body — keep the status-line fallback.
    }
    throw new Error(detail);
  }
  const wire = (await res.json()) as { runner_id: string };
  return { runnerId: wire.runner_id };
}

/**
 * PATCH mutable session properties.
 *
 * `null` on `reasoningEffort` / `modelOverride` sends the server's
 * ``"default"`` clear alias (matches the REPL's ``/effort | /model
 * default``). `null` on `costControlModeOverride` is sent as a JSON
 * ``null`` — for that field, "off" is a real value, so explicit null
 * (not an alias) is the server's clear signal.
 *
 * `silent: true` persists without firing the claude-native tmux
 * forward — use for bind-time auto-apply (e.g. the sticky-pref
 * handoff in `bindStream`) where injecting a visible "/model X"
 * item into a fresh pane would look like an unexpected first
 * message in the chat.
 */
export async function updateSession(
  sessionId: string,
  updates: {
    reasoningEffort?: string | null;
    modelOverride?: string | null;
    codexPlanMode?: boolean;
    costControlModeOverride?: "on" | "off" | null;
    runnerId?: string;
    silent?: boolean;
  },
): Promise<Session> {
  const body: Record<string, string | boolean | null> = {};
  if ("reasoningEffort" in updates) {
    body.reasoning_effort = updates.reasoningEffort ?? "default";
  }
  if ("modelOverride" in updates) {
    body.model_override = updates.modelOverride ?? "default";
  }
  if (updates.codexPlanMode !== undefined) {
    body.collaboration_mode = updates.codexPlanMode ? "plan" : "default";
  }
  if ("costControlModeOverride" in updates) {
    body.cost_control_mode_override = updates.costControlModeOverride ?? null;
  }
  if (updates.runnerId !== undefined) {
    body.runner_id = updates.runnerId;
  }
  if (updates.silent) {
    body.silent = true;
  }
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return sessionFromWire(await readJsonOrThrow<SessionResponseWire>(res));
}

interface RunnerSummaryWire {
  runner_id: string;
  online: boolean;
  harnesses?: string[];
}

interface RunnerSummary {
  runnerId: string;
  online: boolean;
  harnesses: string[];
}

function runnerFromWire(wire: RunnerSummaryWire): RunnerSummary {
  return {
    runnerId: wire.runner_id,
    online: wire.online,
    harnesses: wire.harnesses ?? [],
  };
}

/** List currently online runners known to the server. */
export async function listRunners(): Promise<RunnerSummary[]> {
  const res = await authenticatedFetch("/v1/runners");
  const body = await readJsonOrThrow<{ data?: RunnerSummaryWire[] }>(res);
  return (body.data ?? []).map(runnerFromWire);
}

/**
 * Bind a session to the only online runner, if the choice is
 * unambiguous. Returns null when no runner is online.
 */
export async function bindOnlyOnlineRunner(sessionId: string): Promise<Session | null> {
  const runners = (await listRunners()).filter((runner) => runner.online);
  if (runners.length === 0) return null;
  if (runners.length > 1) {
    throw new Error(`Cannot choose runner: ${runners.length} runners are online`);
  }
  return updateSession(sessionId, { runnerId: runners[0]!.runnerId });
}

/**
 * Snapshot a session. Per the reconnect contract
 * (`omnigent/server/API.md` §Reconnect Contract), callers should
 * open the live stream FIRST, then call this, then dedupe items by
 * `id`. Calling this alone returns committed state at that moment
 * with no transient (delta) coverage.
 */
export async function getSession(sessionId: string): Promise<Session> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}`);
  return sessionFromWire(await readJsonOrThrow<SessionResponseWire>(res));
}

/**
 * Snapshot a session WITHOUT its committed items or liveness fields.
 *
 * Use this (not `getSession`) when the caller hydrates the transcript
 * via `fetchSessionItemsPage` and reads liveness from the /health poll +
 * WS stream — i.e. the chat surface's snapshot consumers. The skipped
 * reads are the two most expensive steps of the server's snapshot build
 * (the 100-item history read and the runner/host liveness lookup), so
 * this is the fast path for open/switch. The returned `Session` has
 * `items: []`; callers that need the snapshot's own items (or
 * `runner_online`/`host_online` once the wire type carries them) must
 * use `getSession` instead. Older servers ignore the params and return
 * the full snapshot — both shapes parse identically.
 *
 * NOTE: keep all consumers of a given react-query key (`["session", id]`)
 * on the SAME variant — mixing full and slim under one key would let a
 * cached slim snapshot serve a caller that expected items.
 */
export interface GetSessionSlimOptions {
  /**
   * Ask the AP server to refresh runner-backed session state before
   * returning the snapshot. Used by page-load/bind paths so a browser
   * refresh pierces stale server-side capability caches.
   */
  refreshState?: boolean;
}

export async function getSessionSlim(
  sessionId: string,
  options: GetSessionSlimOptions = {},
): Promise<Session> {
  const params = new URLSearchParams({
    include_items: "false",
    include_liveness: "false",
  });
  if (options.refreshState === true) params.set("refresh_state", "true");
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}?${params.toString()}`,
  );
  return sessionFromWire(await readJsonOrThrow<SessionResponseWire>(res));
}

/** One page of a session's committed items, in chronological order. */
export interface SessionItemsPage {
  /** Items oldest-to-newest, ready to feed `itemsToBlocks`. */
  items: ConversationItem[];
  /** True when older items exist before the first item in this page. */
  hasMore: boolean;
}

/**
 * Fetch one page of a session's committed items, oldest-to-newest.
 *
 * Opening a conversation hydrates only the most recent page; scroll-up
 * loading passes the oldest loaded item id as `olderThan` to page
 * backwards. This bounds how many items are fetched, parsed, and
 * rendered up front, so opening a long transcript stays fast — full
 * history stays reachable by paging older.
 *
 * The server orders by position, so we request the newest `limit` items
 * (`order=desc`, plus `after` the cursor when paging back) and reverse
 * to chronological. `has_more` reports whether still-older items remain.
 */
export async function fetchSessionItemsPage(
  sessionId: string,
  { olderThan, limit = SESSION_HISTORY_PAGE_SIZE }: { olderThan?: string; limit?: number } = {},
): Promise<SessionItemsPage> {
  const params = new URLSearchParams({ limit: String(limit), order: "desc" });
  // "Older than the cursor" within a descending scan = items after it.
  if (olderThan) params.set("after", olderThan);
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/items?${params}`,
  );
  const page = await readJsonOrThrow<SessionItemsResponseWire>(res);
  // Server returns newest-first; reverse to chronological for rendering.
  return { items: [...page.data].reverse(), hasMore: page.has_more };
}

/**
 * Upper bound on pages `fetchInitialHistoryWindow` will fetch before
 * giving up on reaching the previous-user-message boundary. Caps a
 * pathological single turn (thousands of tool calls between two user
 * prompts) from fanning out into unbounded requests on open. When the
 * cap is hit we stop with `hasMore: true`, so the rest stays reachable
 * via scroll-up `loadMoreHistory` — not a silent truncation.
 */
const MAX_INITIAL_PAGES = 8;

/** A real (non-meta) user prompt — the boundary the initial window snaps to. */
function isUserPrompt(item: ConversationItem): boolean {
  return isMessageItem(item) && item.role === "user" && !item.is_meta;
}

function initialHistoryBoundaryReached(items: ConversationItem[], hasMore: boolean): boolean {
  if (!hasMore) return true;
  const userCount = items.filter(isUserPrompt).length;
  return items.length >= SESSION_HISTORY_PAGE_SIZE && userCount >= 2;
}

/**
 * Fetch exactly one newest history page for fast first paint on switch.
 */
export function fetchInitialHistoryFirstPage(sessionId: string): Promise<SessionItemsPage> {
  return fetchSessionItemsPage(sessionId);
}

/**
 * Continue paging older history until the previous-user boundary is reached.
 *
 * Starts from a first page (typically already rendered) so callers can paint
 * immediately and expand the initial window asynchronously.
 */
export async function extendInitialHistoryWindow(
  sessionId: string,
  seedPage: SessionItemsPage,
): Promise<SessionItemsPage> {
  let items = [...seedPage.items];
  let hasMore = seedPage.hasMore;
  if (initialHistoryBoundaryReached(items, hasMore)) return { items, hasMore };

  for (let pages = 1; pages < MAX_INITIAL_PAGES; pages++) {
    const cursor = items[0]?.id;
    if (!cursor) break;
    // Cursor pagination is stateful; each request depends on the prior page.
    // eslint-disable-next-line no-await-in-loop
    const page = await fetchSessionItemsPage(sessionId, { olderThan: cursor });
    items = [...page.items, ...items]; // prepend the older page
    hasMore = page.hasMore;
    if (initialHistoryBoundaryReached(items, hasMore)) break;
  }

  return { items, hasMore };
}

/**
 * Hydrate the initial conversation window: at least
 * `SESSION_HISTORY_PAGE_SIZE` items, but extended further back when
 * needed so the *previous* user prompt is included — i.e.
 * `max(one page, back-to-previous-user-message)`.
 *
 * Why: the flat page size can land mid-turn for a long turn (many tool
 * calls after the last user message), so the user opens the chat to a
 * response with no visible prompt above it. We page backward until we've
 * collected two non-meta user messages (the last turn's prompt plus the
 * one before it) AND met the item floor, so the last full exchange and
 * its preceding prompt are always on screen.
 *
 * Cost: the common case (a page that already holds ≥2 user prompts) is a
 * single request, identical to `fetchSessionItemsPage`. Extra requests
 * fire only for long single turns — exactly the case this targets.
 * Bounded by `MAX_INITIAL_PAGES`.
 *
 * Returns the same `{ items, hasMore }` shape as `fetchSessionItemsPage`
 * so callers feed `oldestItemId` / `hasMoreHistory` from it unchanged.
 */
export async function fetchInitialHistoryWindow(sessionId: string): Promise<SessionItemsPage> {
  const firstPage = await fetchInitialHistoryFirstPage(sessionId);
  return extendInitialHistoryWindow(sessionId, firstPage);
}

/**
 * Flatten a `GET /v1/sessions/{id}` item into the flat
 * `ConversationItem` shape used by `itemsToBlocks`.
 *
 * The sessions snapshot serializes items as
 * `{id, type, status, response_id, created_at, data: {...}}` (Pydantic
 * model), while `GET /v1/conversations/{id}/items` flattens via
 * `to_api_dict()` to `{id, type, status, response_id, ...fields}`.
 * `itemsToBlocks` expects the flat shape. Spread `data` up to the top
 * level on flat items; pass through items that already look flat.
 *
 * Follow-up: align the server to use `to_api_dict()` in the session
 * snapshot route — once that lands, this helper is dead code.
 */
export function flattenSessionItem(item: SessionItem): ConversationItem {
  if (!isNestedSessionItem(item)) return item;
  const { data, ...rest } = item;
  return { ...rest, ...data } as ConversationItem;
}

function isNestedSessionItem(item: SessionItem): item is NestedSessionItem {
  return "data" in item && typeof (item as NestedSessionItem).data === "object";
}

/**
 * Submit a single event to a session. Validated server-side per the
 * `type` discriminator (`message`, `function_call_output`,
 * `interrupt`, `approval`, etc.); item-typed events are persisted
 * synchronously before the route returns, then `session.input.consumed`
 * fires on the live stream.
 */
export async function postEvent(
  sessionId: string,
  event: SessionEventInput,
): Promise<PostEventResponse> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/events`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(event),
  });
  // Throw a typed ApiError (not the bare status line) so callers can branch
  // on `code` — e.g. surface a friendly "runner didn't come online" message
  // for the 503 the server returns when a host-bound runner never connects.
  if (!res.ok) throw await apiErrorFromResponse(res);
  return postEventResponseFromWire(
    (await res.json()) as {
      queued: boolean;
      item_id?: string;
      denied?: boolean;
      pending_id?: string;
    },
  );
}

/**
 * Open the session live-tail SSE stream. Returns the raw fetch
 * `Response` so callers can pipe `res.body` through `parseSseStream`
 * (in `./sse`). The caller is responsible for aborting the request
 * via the supplied signal (on `switchTo` / tab unload).
 *
 * Holding this stream open registers the user as a session *viewer*
 * (presence circles). `opts.idle` is the connect-time presence idle
 * flag — the stream URL is the entire presence uplink, so an idle
 * flip mid-view arrives as a reconnect carrying the new value.
 *
 * The fetch itself throws only on network failure; HTTP errors
 * surface as `res.ok === false`. Callers must check `res.ok` before
 * consuming the body.
 */
export function openSessionStream(
  sessionId: string,
  signal: AbortSignal,
  opts?: { idle?: boolean },
): Promise<Response> {
  const query = opts?.idle ? "?idle=true" : "";
  return authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/stream${query}`, {
    headers: { Accept: "text/event-stream" },
    signal,
  });
}

/**
 * Send a user-initiated cancel into the session. Co-emits
 * `session.interrupted` (transient) and `response.incomplete` (with
 * `incomplete_details.reason == "user_interrupt"`) on the live
 * stream — clients can mark the bubble interrupted from either.
 */
export function interrupt(sessionId: string): Promise<PostEventResponse> {
  return postEvent(sessionId, { type: "interrupt", data: {} });
}

/**
 * Terminate a live session without deleting its conversation. The
 * transcript stays viewable; only the running process is stopped.
 * Owner-only server-side. For claude-native sessions the bound runner
 * hard-kills the tmux pane the `claude` binary runs in — the analog
 * of exiting from inside tmux, but driven from the web UI.
 */
export function stopSession(sessionId: string): Promise<PostEventResponse> {
  return postEvent(sessionId, { type: "stop_session", data: {} });
}

/**
 * Resolve an outstanding elicitation via its dedicated URL endpoint
 * (URL-based elicitation): `POST /v1/sessions/{id}/elicitations/{eid}/resolve`
 * with the MCP-shape `ElicitResult` body. The elicitation id rides in
 * the URL path, not the body.
 *
 * Same effect as the legacy `approval` session event — both converge on
 * the server's `_resolve_elicitation` (harness Future + sidebar clear +
 * runner forward) — but routing the verdict through the resource-scoped
 * URL keeps human approval on a dedicated, owner-gated path rather than
 * an in-band session event. The `ApprovalCard` UX is unchanged; only the
 * submit target moves.
 */
export async function approve(
  sessionId: string,
  elicitationId: string,
  result: ElicitResult,
): Promise<PostEventResponse> {
  const res = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/elicitations/` +
      `${encodeURIComponent(elicitationId)}/resolve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(result),
    },
  );
  return postEventResponseFromWire(
    await readJsonOrThrow<{ queued: boolean; item_id?: string }>(res),
  );
}

/** React Query key for the initial history window prefetched on sidebar hover. */
export function initialHistoryQueryKey(sessionId: string): readonly [string, string, string] {
  return ["session", sessionId, "initialHistory"];
}

/**
 * Warm the session snapshot and initial history cache before a sidebar click.
 * TanStack dedupes in-flight prefetches with `bindStream`'s fetches.
 */
export function prefetchSessionForSwitch(queryClient: QueryClient, sessionId: string): void {
  const snapshotState = queryClient.getQueryState(["session", sessionId]);
  if (snapshotState?.fetchStatus !== "fetching") {
    void queryClient.prefetchQuery({
      queryKey: ["session", sessionId],
      queryFn: () => getSessionSlim(sessionId, { refreshState: true }),
      staleTime: 30_000,
    });
  }
  const historyState = queryClient.getQueryState(initialHistoryQueryKey(sessionId));
  if (historyState?.fetchStatus !== "fetching") {
    void queryClient.prefetchQuery({
      queryKey: initialHistoryQueryKey(sessionId),
      queryFn: () => fetchInitialHistoryFirstPage(sessionId),
      staleTime: 60_000,
    });
  }
}
