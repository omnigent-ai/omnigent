// Hand-written client for the six `/v1/scheduled-tasks` endpoints, mirroring
// `omnigent/server/routes/scheduled_tasks.py`. Follows the same conventions as
// the sibling `sessionsApi.ts`: requests go through the Vite `/v1` proxy, the
// wire is snake_case while the TS surface is camelCase, and non-OK responses
// throw a typed error carrying the server's machine-readable `code`.
//
// A scheduled task is a saved prompt that fires an agent session on a recurring
// RFC-5545 RRULE schedule. `host_id` / `workspace` are optional (post-FU-3):
// omit both and the server resolves the owner's connected host + its home dir
// at fire time; supply them only as a validated pair.

import { authenticatedFetch } from "./identity";

/** Lifecycle state of a scheduled task. `paused` tasks don't fire. */
export type ScheduledTaskState = "active" | "paused";

/** Terminal + in-flight statuses a single run can hold. */
export type ScheduledTaskRunStatus =
  | "scheduled"
  | "running"
  | "succeeded"
  | "failed"
  | "skipped"
  | "incomplete";

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
  /** Pinned absolute workspace, or `null` (server defaults to the host home). */
  workspace: string | null;
  /** Pinned host, or `null` (server resolves the connected host at fire time). */
  hostId: string | null;
  state: ScheduledTaskState;
  /** Epoch seconds of the last fire, or `null` if it has never fired. */
  lastRunAt: number | null;
  lastRunConversationId: string | null;
}

/** One run of a scheduled task, camelCased from `_run_to_response`. */
export interface ScheduledTaskRun {
  id: string;
  scheduledTaskId: string;
  status: ScheduledTaskRunStatus;
  scheduledAt: number;
  conversationId: string | null;
  firedAt: number | null;
  finishedAt: number | null;
  /** Queryable failure classification, e.g. `no_online_host`; `null` on success. */
  errorCode: string | null;
}

/** Body for `POST /v1/scheduled-tasks`. */
export interface CreateScheduledTaskInput {
  name: string;
  prompt: string;
  rrule: string;
  agentId: string;
  timezone?: string;
  modelOverride?: string | null;
  reasoningEffort?: string | null;
  /** Optional pinned workspace; only valid together with `hostId`. */
  workspace?: string | null;
  /** Optional pinned host. */
  hostId?: string | null;
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
  timezone?: string;
  modelOverride?: string | null;
  reasoningEffort?: string | null;
  workspace?: string;
  hostId?: string;
  state?: ScheduledTaskState;
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
  workspace: string | null;
  host_id: string | null;
  state: ScheduledTaskState;
  last_run_at: number | null;
  last_run_conversation_id: string | null;
}

/** Wire shape of a run row (snake_case), matching `_run_to_response`. */
interface ScheduledTaskRunWire {
  id: string;
  scheduled_task_id: string;
  status: ScheduledTaskRunStatus;
  scheduled_at: number;
  conversation_id: string | null;
  fired_at: number | null;
  finished_at: number | null;
  error_code: string | null;
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
    workspace: wire.workspace,
    hostId: wire.host_id,
    state: wire.state,
    lastRunAt: wire.last_run_at,
    lastRunConversationId: wire.last_run_conversation_id,
  };
}

function runFromWire(wire: ScheduledTaskRunWire): ScheduledTaskRun {
  return {
    id: wire.id,
    scheduledTaskId: wire.scheduled_task_id,
    status: wire.status,
    scheduledAt: wire.scheduled_at,
    conversationId: wire.conversation_id,
    firedAt: wire.fired_at,
    finishedAt: wire.finished_at,
    errorCode: wire.error_code,
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
  if (input.workspace != null) body.workspace = input.workspace;
  if (input.hostId != null) body.host_id = input.hostId;
  const res = await authenticatedFetch("/v1/scheduled-tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return taskFromWire(await readJsonOrThrow<ScheduledTaskWire>(res));
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
  if (input.timezone !== undefined) body.timezone = input.timezone;
  if (input.modelOverride !== undefined) body.model_override = input.modelOverride;
  if (input.reasoningEffort !== undefined) body.reasoning_effort = input.reasoningEffort;
  if (input.workspace !== undefined) body.workspace = input.workspace;
  if (input.hostId !== undefined) body.host_id = input.hostId;
  if (input.state !== undefined) body.state = input.state;
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
