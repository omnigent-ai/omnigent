// Hand-written client for the six `/v1/scheduled-tasks` endpoints, mirroring
// `omnigent/server/routes/scheduled_tasks.py`. Follows the same conventions as
// the sibling `sessionsApi.ts`: requests go through the Vite `/v1` proxy, the
// wire is snake_case while the TS surface is camelCase, and non-OK responses
// throw a typed error carrying the server's machine-readable `code`.
//
// A scheduled task is a saved prompt that fires an agent session on a recurring
// RFC-5545 RRULE schedule. `host_id` / `workspace` are optional (post-FU-3):
// omit both and the server resolves the owner's connected host + its home dir
// at fire time. Sandbox tasks instead accept a repository URL as workspace.

import { authenticatedFetch } from "./identity";
import { startTimedInteraction } from "./analyticsEmit";

export const RANDOM_WORKTREE_BASE_REF = "HEAD";

/** Lifecycle state of a scheduled task. `paused` tasks don't fire. */
export type ScheduledTaskState = "active" | "paused";

/**
 * Where each firing runs. `connected_host` pins/resolves the owner's own
 * machine; `managed_sandbox` provisions a FRESH server-managed sandbox per
 * firing (no host, optional repository URL), using the server's normal sandbox lifecycle.
 */
export type ScheduledTaskExecutionTarget = "connected_host" | "managed_sandbox";

/** Whether each run starts a new session or continues one. */
export type ScheduledTaskSessionMode = "new" | "continue";

/** Terminal + in-flight statuses a single run can hold. */
export type ScheduledTaskRunStatus =
  "scheduled" | "starting" | "running" | "succeeded" | "failed" | "skipped" | "incomplete";

/**
 * A scheduled task, camelCased from the server's `_to_response` shape. The
 * server preserves the JSON key `owner_user_id` for the owning user even though
 * the DB column is `user_id`; it's surfaced here as `ownerUserId`.
 */
export interface ScheduledTask {
  id: string;
  name: string;
  prompt: string;
  /** RFC-5545 recurrence rule, e.g. `FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=8;BYMINUTE=0`. */
  rrule: string;
  /** Owning user, or `null` in single-user / auth-disabled deployments. */
  ownerUserId: string | null;
  agentId: string;
  /** IANA timezone the RRULE is evaluated in, e.g. `America/Los_Angeles`. */
  timezone: string;
  createdAt: number;
  updatedAt: number;
  modelOverride: string | null;
  reasoningEffort: string | null;
  /**
   * Harness-specific permission or execution mode, e.g. `acceptEdits`, or `null`
   * to use the agent's configured default. The server derives the runner's
   * `--permission-mode` launch arg from it at fire time.
   */
  permissionMode: string | null;
  /** `"continue"` sends every run's prompt into `sessionId` instead of a new session. */
  sessionMode: ScheduledTaskSessionMode;
  /** The session a `"continue"` task sends to; `null` until its first run creates one. */
  sessionId: string | null;
  /** Host directory or sandbox repository URL; null uses the target default. */
  workspace: string | null;
  /** Base ref for a fresh random worktree; null/absent uses the selected directory. */
  baseBranch?: string | null;
  /** Pinned host, or `null` (server resolves the connected host at fire time). */
  hostId: string | null;
  /** Where firings run — a connected host, or a fresh managed sandbox per fire. */
  executionTarget: ScheduledTaskExecutionTarget;
  /** Project every fired session is filed into, or `null` to leave them unfiled. */
  projectId: string | null;
  state: ScheduledTaskState;
  /** Epoch seconds of the last fire, or `null` if it has never fired. */
  lastRunAt: number | null;
  /**
   * Status of the task's MOST RECENT run (`succeeded` / `failed` / `skipped` /
   * `starting` / `running` / `scheduled` / `incomplete`), or `null` when the task has never
   * run. Server-computed from the latest run row so the list can show a
   * completion badge without a per-row `/runs` fetch.
   */
  lastRunStatus: ScheduledTaskRunStatus | null;
  lastRunConversationId: string | null;
  /**
   * ISO-8601 timestamp of the next scheduled fire, computed by the SERVER's
   * live scheduler (its authoritative anchor), or `null` when the task is
   * paused / not armed. Never recompute this on the client — a client-computed
   * next-run can't match the server anchor for INTERVAL>1 rules.
   */
  nextRunAt: string | null;
}

/** One run of a scheduled task, camelCased from `_run_to_response`. */
export interface ScheduledTaskRun {
  /** Current cumulative session cost; null when not reported (not a per-run delta). */
  sessionCostUsd: number | null;
  /** Current session approvals, shared by every run using that session. */
  conversationPendingApprovals?: number;
  id: string;
  scheduledTaskId: string;
  status: ScheduledTaskRunStatus;
  scheduledAt: number;
  conversationId: string | null;
  firedAt: number | null;
  finishedAt: number | null;
  /** Queryable failure classification, e.g. `no_online_host`; `null` on success. */
  errorCode: string | null;
  /** Epoch seconds of the conversation's last update; `null` for runs without a conversation. */
  conversationUpdatedAt: number | null;
  /**
   * The session's status as `GET /v1/sessions` reports it (`"idle"`, `"running"` or
   * `"failed"`), without that list's sub-agent roll-up; `null` for runs without a conversation.
   */
  conversationStatus: string | null;
  /**
   * When the caller last saw the conversation (epoch seconds), as `viewer_last_seen` in
   * `GET /v1/sessions`; `null` if never, or for runs without a conversation.
   */
  viewerLastSeen: number | null;
  /**
   * Whether the caller has explicitly marked this conversation unread (server-side
   * `viewer_unread`); `null` for runs without a conversation. Hydrates
   * `useUnseenConversations` on load so the dot can be computed without per-row fetches.
   */
  viewerUnread: boolean | null;
}

/** Fixed analytics labels; keep in sync with omnigent/server/scheduled/telemetry.py. */
export const AUTOMATION_TEMPLATE_IDS = [
  "custom",
  "jira",
  "slack",
  "github",
  "linear",
  "follow-up-monitor",
  "pr-sweep",
  "news-digest",
] as const;
export type AutomationTemplateId = (typeof AUTOMATION_TEMPLATE_IDS)[number];

/** Body for `POST /v1/scheduled-tasks`. */
export interface CreateScheduledTaskInput {
  /** Diagnostic creation attribution, sent as headers for older-server compatibility. */
  templateId?: AutomationTemplateId;
  name: string;
  prompt: string;
  rrule: string;
  agentId: string;
  timezone?: string;
  modelOverride?: string | null;
  reasoningEffort?: string | null;
  /** Harness-specific permission or execution mode; omit for the agent default. */
  permissionMode?: string | null;
  /** `"continue"` keeps one session across runs; omit for a new session per run. */
  sessionMode?: ScheduledTaskSessionMode;
  /** Host directory (requires hostId), or a repository URL for a sandbox. */
  workspace?: string | null;
  baseBranch?: string | null;
  /** Optional pinned host. */
  hostId?: string | null;
  /**
   * Where firings run. Omit (or `connected_host`) for the connected-host
   * behavior; `managed_sandbox` provisions a fresh sandbox each fire and must
   * NOT be combined with `hostId`. Its workspace is an optional repository URL.
   */
  executionTarget?: ScheduledTaskExecutionTarget;
  /** File every fired session into this existing project. */
  projectId?: string | null;
}

/**
 * Body for `PATCH /v1/scheduled-tasks/{id}`. Every field is optional; only the
 * fields present are changed. The server rejects nulling an already-set
 * `workspace` / `hostId` — send a new value or leave the field unset.
 */
export interface UpdateScheduledTaskInput {
  name?: string;
  prompt?: string;
  rrule?: string;
  /**
   * Rebind the task to a different agent, switching the harness its future
   * firings run. The server clears `modelOverride` / `reasoningEffort` /
   * `permissionMode` on a switch (a model id is provider-bound, permission mode
   * is Claude-only) unless the same PATCH resends them.
   */
  agentId?: string;
  timezone?: string;
  modelOverride?: string | null;
  reasoningEffort?: string | null;
  permissionMode?: string | null;
  sessionMode?: ScheduledTaskSessionMode;
  /** `null` makes a `"continue"` task's next run start the session later runs continue. */
  sessionId?: string | null;
  /** Host directory or sandbox repository; null restores a sandbox provider's default. */
  workspace?: string | null;
  baseBranch?: string | null;
  hostId?: string;
  /**
   * Switch where firings run. `managed_sandbox` clears any pinned host; do not
   * also set `hostId` in the same update. Changing targets clears the previous
   * workspace unless a replacement is supplied.
   */
  executionTarget?: ScheduledTaskExecutionTarget;
  state?: ScheduledTaskState;
  /** File future fired sessions into this existing project; `null` unfiles them. */
  projectId?: string | null;
}

/** Wire shape of a task row (snake_case), matching `_to_response`. */
interface ScheduledTaskWire {
  id: string;
  name: string;
  prompt: string;
  rrule: string;
  owner_user_id: string | null;
  agent_id: string;
  timezone: string;
  created_at: number;
  updated_at: number;
  model_override: string | null;
  reasoning_effort: string | null;
  permission_mode: string | null;
  session_mode?: ScheduledTaskSessionMode;
  session_id?: string | null;
  workspace: string | null;
  base_branch?: string | null;
  host_id: string | null;
  execution_target: ScheduledTaskExecutionTarget;
  // Absent on servers that predate task projects.
  project_id?: string | null;
  state: ScheduledTaskState;
  last_run_at: number | null;
  last_run_status: ScheduledTaskRunStatus | null;
  last_run_conversation_id: string | null;
  next_run_at: string | null;
}

/** Wire shape of a run row (snake_case), matching `_run_to_response`. */
interface ScheduledTaskRunWire {
  session_cost_usd?: number | null;
  conversation_pending_approvals?: number;
  id: string;
  scheduled_task_id: string;
  status: ScheduledTaskRunStatus;
  scheduled_at: number;
  conversation_id: string | null;
  fired_at: number | null;
  finished_at: number | null;
  error_code: string | null;
  conversation_updated_at: number | null;
  conversation_status: string | null;
  viewer_last_seen: number | null;
  viewer_unread: boolean | null;
}

/**
 * A typed HTTP error carrying the server's machine-readable `code`, so callers
 * can branch on the failure kind (e.g. `invalid_input` for a bad RRULE) rather
 * than string-matching the message. Mirrors `ApiError` in `sessionsApi.ts`;
 * kept local so this module has no cross-import on the sessions client.
 */
export class ScheduledTaskApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  constructor(message: string, status: number, code: string | null) {
    super(message);
    this.name = "ScheduledTaskApiError";
    this.status = status;
    this.code = code;
  }
}

/**
 * Build a {@link ScheduledTaskApiError} from a non-OK response, preferring the
 * server's `error.message` / `error.code` (the `OmnigentError` shape) over the
 * bare status line.
 */
async function errorFromResponse(res: Response): Promise<ScheduledTaskApiError> {
  let message = `${res.status} ${res.statusText}`;
  let code: string | null = null;
  try {
    const body = (await res.json()) as { error?: { code?: string; message?: string } };
    if (body.error?.message) message = body.error.message;
    if (body.error?.code) code = body.error.code;
  } catch {
    // Non-JSON / empty body — keep the status-line fallback.
  }
  return new ScheduledTaskApiError(message, res.status, code);
}

async function readJsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) throw await errorFromResponse(res);
  return (await res.json()) as T;
}

function taskFromWire(wire: ScheduledTaskWire): ScheduledTask {
  return {
    id: wire.id,
    name: wire.name,
    prompt: wire.prompt,
    rrule: wire.rrule,
    ownerUserId: wire.owner_user_id,
    agentId: wire.agent_id,
    timezone: wire.timezone,
    createdAt: wire.created_at,
    updatedAt: wire.updated_at,
    modelOverride: wire.model_override,
    reasoningEffort: wire.reasoning_effort,
    permissionMode: wire.permission_mode,
    sessionMode: wire.session_mode ?? "new",
    sessionId: wire.session_id ?? null,
    workspace: wire.workspace,
    baseBranch: wire.base_branch ?? null,
    hostId: wire.host_id,
    executionTarget: wire.execution_target,
    projectId: wire.project_id ?? null,
    state: wire.state,
    lastRunAt: wire.last_run_at,
    lastRunStatus: wire.last_run_status,
    lastRunConversationId: wire.last_run_conversation_id,
    nextRunAt: wire.next_run_at,
  };
}

function runFromWire(wire: ScheduledTaskRunWire): ScheduledTaskRun {
  return {
    sessionCostUsd: wire.session_cost_usd ?? null,
    conversationPendingApprovals: wire.conversation_pending_approvals ?? 0,
    id: wire.id,
    scheduledTaskId: wire.scheduled_task_id,
    status: wire.status,
    scheduledAt: wire.scheduled_at,
    conversationId: wire.conversation_id,
    firedAt: wire.fired_at,
    finishedAt: wire.finished_at,
    errorCode: wire.error_code,
    conversationUpdatedAt: wire.conversation_updated_at ?? null,
    conversationStatus: wire.conversation_status ?? null,
    viewerLastSeen: wire.viewer_last_seen ?? null,
    viewerUnread: wire.viewer_unread ?? null,
  };
}

/** List the caller's scheduled tasks (owner-scoped server-side). */
export async function listScheduledTasks(): Promise<ScheduledTask[]> {
  const res = await authenticatedFetch("/v1/scheduled-tasks");
  const body = await readJsonOrThrow<{ scheduled_tasks: ScheduledTaskWire[] }>(res);
  return (body.scheduled_tasks ?? []).map(taskFromWire);
}

/** Fetch one of the caller's scheduled tasks (404 if not owned). */
export async function getScheduledTask(id: string): Promise<ScheduledTask> {
  const res = await authenticatedFetch(`/v1/scheduled-tasks/${encodeURIComponent(id)}`);
  return taskFromWire(await readJsonOrThrow<ScheduledTaskWire>(res));
}

/** List a task's run history, most-recent-first (404 if not owned). */
export async function listScheduledTaskRuns(id: string): Promise<ScheduledTaskRun[]> {
  const res = await authenticatedFetch(`/v1/scheduled-tasks/${encodeURIComponent(id)}/runs`);
  const body = await readJsonOrThrow<{ runs: ScheduledTaskRunWire[] }>(res);
  return (body.runs ?? []).map(runFromWire);
}

/**
 * Create a scheduled task. Only the fields the user actually set are sent, so
 * the server applies its own defaults (`timezone` → UTC) and the optional
 * host/workspace pair is omitted entirely when unset (server resolves the
 * connected host at fire time).
 */
export async function createScheduledTask(input: CreateScheduledTaskInput): Promise<ScheduledTask> {
  const body: Record<string, unknown> = {
    name: input.name,
    prompt: input.prompt,
    rrule: input.rrule,
    agent_id: input.agentId,
  };
  if (input.timezone !== undefined) body.timezone = input.timezone;
  if (input.modelOverride != null) body.model_override = input.modelOverride;
  if (input.reasoningEffort != null) body.reasoning_effort = input.reasoningEffort;
  if (input.permissionMode != null) body.permission_mode = input.permissionMode;
  if (input.sessionMode !== undefined) body.session_mode = input.sessionMode;
  if (input.baseBranch !== undefined) body.base_branch = input.baseBranch;
  if (input.workspace != null) body.workspace = input.workspace;
  if (input.hostId != null) body.host_id = input.hostId;
  if (input.executionTarget !== undefined) body.execution_target = input.executionTarget;
  if (input.projectId != null) body.project_id = input.projectId;
  const templateId = input.templateId ?? "custom";
  const interaction = startTimedInteraction("create_scheduled_task", undefined, templateId);
  try {
    const res = await authenticatedFetch("/v1/scheduled-tasks", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-omnigent-automation-source": "ui",
        "x-omnigent-automation-template": templateId,
      },
      body: JSON.stringify(body),
    });
    const task = taskFromWire(await readJsonOrThrow<ScheduledTaskWire>(res));
    interaction.complete();
    return task;
  } catch (error) {
    interaction.fail();
    throw error;
  }
}

/**
 * Update mutable fields of a task (pause/reactivate/rename/reschedule). Only
 * the keys present in `input` are sent, matching the server's
 * `exclude_unset` semantics.
 */
export async function updateScheduledTask(
  id: string,
  input: UpdateScheduledTaskInput,
): Promise<ScheduledTask> {
  const body: Record<string, unknown> = {};
  if (input.name !== undefined) body.name = input.name;
  if (input.prompt !== undefined) body.prompt = input.prompt;
  if (input.rrule !== undefined) body.rrule = input.rrule;
  if (input.agentId !== undefined) body.agent_id = input.agentId;
  if (input.timezone !== undefined) body.timezone = input.timezone;
  if (input.modelOverride !== undefined) body.model_override = input.modelOverride;
  if (input.reasoningEffort !== undefined) body.reasoning_effort = input.reasoningEffort;
  if (input.permissionMode !== undefined) body.permission_mode = input.permissionMode;
  if (input.sessionMode !== undefined) body.session_mode = input.sessionMode;
  if (input.baseBranch !== undefined) body.base_branch = input.baseBranch;
  if (input.sessionId !== undefined) body.session_id = input.sessionId;
  if (input.workspace !== undefined) body.workspace = input.workspace;
  if (input.hostId !== undefined) body.host_id = input.hostId;
  if (input.executionTarget !== undefined) body.execution_target = input.executionTarget;
  if (input.state !== undefined) body.state = input.state;
  if (input.projectId !== undefined) body.project_id = input.projectId;
  const res = await authenticatedFetch(`/v1/scheduled-tasks/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return taskFromWire(await readJsonOrThrow<ScheduledTaskWire>(res));
}

/** Delete a task so it no longer fires. */
export async function deleteScheduledTask(id: string): Promise<void> {
  const res = await authenticatedFetch(`/v1/scheduled-tasks/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw await errorFromResponse(res);
}

/**
 * Trigger an immediate ("run now") fire of a task. A manual override that
 * reuses the server's shared fire path; paused tasks are runnable. The server
 * responds `202 Accepted` with the queued run ID, or no run ID on older servers.
 * Callers refresh the list and runs queries for its status. A `409` (already
 * in flight) surfaces as a {@link ScheduledTaskApiError}.
 */
export async function runScheduledTaskNow(id: string): Promise<string | null> {
  const res = await authenticatedFetch(`/v1/scheduled-tasks/${encodeURIComponent(id)}/run`, {
    method: "POST",
  });
  if (!res.ok) throw await errorFromResponse(res);
  const body = (await res.json()) as { run_id?: string };
  return body.run_id ?? null;
}

/** Latest failed run from an automation without a session Inbox entry. */
interface AutomationFailureWire {
  task_id: string;
  task_name: string;
  run_id: string;
  scheduled_at: number;
  finished_at: number | null;
  error_code: string | null;
}

export interface AutomationFailure {
  taskId: string;
  taskName: string;
  runId: string;
  scheduledAt: number;
  finishedAt: number | null;
  errorCode: string | null;
}

export interface AutomationFailurePage {
  data: AutomationFailure[];
  nextCursor: string | null;
}

function failureFromWire(wire: AutomationFailureWire): AutomationFailure {
  return {
    taskId: wire.task_id,
    taskName: wire.task_name,
    runId: wire.run_id,
    scheduledAt: wire.scheduled_at,
    finishedAt: wire.finished_at,
    errorCode: wire.error_code,
  };
}

const AUTOMATION_INBOX_PAGE_SIZE = 50;

/** Pages over owned tasks; an empty result can still have a continuation. */
export async function listAutomationFailures(after: string | null): Promise<AutomationFailurePage> {
  const params = new URLSearchParams({ limit: String(AUTOMATION_INBOX_PAGE_SIZE) });
  if (after !== null) params.set("after", after);
  const response = await authenticatedFetch(`/v1/scheduled-tasks/inbox?${params}`);
  const page = await readJsonOrThrow<{
    data: AutomationFailureWire[];
    next_cursor: string | null;
  }>(response);
  return { data: page.data.map(failureFromWire), nextCursor: page.next_cursor };
}
