// A session's journal: the plan its agent follows, step statuses and notes.
// The server pages it by position; the session stream pushes new entries.
// The UI shows a plan's steps as tasks, and falls back to the agent's own
// task list when the session has no plan.

import { authenticatedFetch } from "@/lib/identity";
import { relativeTime } from "@/lib/relativeTime";

export type JournalEntryKind =
  "status" | "note" | "dispatch" | "dispatch_result" | "plan" | "plan_status";

export type StepStatus = "started" | "done" | "needs_input" | "failed";

const STEP_STATUSES: readonly string[] = ["started", "done", "needs_input", "failed"];

/** What a task waiting for the user says, in pink: the sidebar's Needs response color. */
export const WAITING_FOR_YOU = "Waiting for you";

/** What an agent last wrote about its work: a step's status and the few words it gave. */
export interface StatusText {
  status: StepStatus;
  text: string;
}

/**
 * The newest status text among *statuses*: the words the agent gave for the
 * status it wrote last, e.g. "Opened 2 PRs". A status without text, such as
 * `started`, doesn't count. `null` when no status has any.
 *
 * @param statuses - Status entries, e.g. each step's newest from a run's plan.
 */
export function latestStatusText(statuses: readonly JournalEntry[]): StatusText | null {
  let newest: JournalEntry | null = null;
  let found: StatusText | null = null;
  for (const entry of statuses) {
    const status = statusOf(entry);
    const text = textOf(entry)?.trim();
    if (entry.kind !== "status" || !status || !text) continue;
    if (newest === null || entry.position > newest.position) {
      newest = entry;
      found = { status, text };
    }
  }
  return found;
}

/** One plan step, as a plan entry's payload lists it. */
export interface JournalPlanStep {
  id: string;
  name: string;
  goal: string | null;
  depends_on: string[];
}

/** One entry of a session's journal, as `GET /v1/sessions/{id}/journal` returns it. */
export interface JournalEntry {
  id: string;
  position: number;
  /** Epoch seconds. */
  created_at: number;
  scheduled_task_run_id: string | null;
  step: string | null;
  author: { type: "agent" | "omnigent" };
  kind: JournalEntryKind;
  payload: Record<string, unknown>;
  /** The saved workflow a plan runs or was saved as, and the entries written under it; absent from older servers. */
  workflow_id?: string | null;
}

export interface JournalPage {
  object: "list";
  data: JournalEntry[];
  first_id: string | null;
  last_id: string | null;
  has_more: boolean;
}

export function sessionJournalKey(conversationId: string | undefined) {
  return ["session-journal", conversationId] as const;
}

/**
 * Fetch one page of the session's journal.
 *
 * @param query - e.g. `{ order: "desc", limit: "100" }`, or `{ order: "asc",
 *   after: "<entry id>" }` to catch up after a reconnect.
 */
export async function fetchJournalPage(
  conversationId: string,
  query: Record<string, string>,
): Promise<JournalPage> {
  const params = new URLSearchParams(query);
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}/journal?${params}`,
  );
  if (!response.ok) throw new Error(`Couldn't load the session's tasks (${response.status})`);
  return (await response.json()) as JournalPage;
}

/** Entries in position order, without duplicates; later copies of an entry win. */
export function mergeJournalEntries(...lists: readonly JournalEntry[][]): JournalEntry[] {
  const byId = new Map<string, JournalEntry>();
  for (const list of lists) for (const entry of list) byId.set(entry.id, entry);
  return [...byId.values()].sort((a, b) => a.position - b.position);
}

export function statusOf(entry: JournalEntry): StepStatus | null {
  const status = entry.payload.status;
  return typeof status === "string" && STEP_STATUSES.includes(status)
    ? (status as StepStatus)
    : null;
}

export function textOf(entry: JournalEntry): string | null {
  return typeof entry.payload.text === "string" ? entry.payload.text : null;
}

export function planSteps(entry: JournalEntry): JournalPlanStep[] {
  const steps = entry.payload.steps;
  return Array.isArray(steps) ? (steps as JournalPlanStep[]) : [];
}

/** A plan's name, its goal in a few words, e.g. "Cut GET /orders p95 by 20%". */
export function planName(entry: JournalEntry | null): string | null {
  const name = entry?.payload.name;
  return typeof name === "string" && name.trim() ? name : null;
}

/** The label a step's status shows. A started step runs only while the session works. */
export function stepStatusLabel(
  status: StepStatus,
  createdAtSeconds: number,
  sessionWorking: boolean,
  nowMs: number = Date.now(),
): string {
  if (status === "started") {
    if (sessionWorking) return "Running";
    const ago = relativeTime(createdAtSeconds * 1000, nowMs);
    return ago === "now" ? "Started just now" : `Started ${ago} ago`;
  }
  return { done: "Done", needs_input: WAITING_FOR_YOU, failed: "Failed" }[status];
}

/** Whether *entry* is the session's first plan, which the agent writes when it starts a workflow. */
export function isFirstPlan(entry: JournalEntry): boolean {
  return entry.kind === "plan" && entry.payload.version === 1;
}

/** The newest plan among the entries, which are in position order. */
export function currentPlan(entries: readonly JournalEntry[]): JournalEntry | null {
  return entries.findLast((entry) => entry.kind === "plan") ?? null;
}

/**
 * The saved workflow the current plan came from. Only the plan that starts a
 * run names it, so a replan that keeps one of that plan's steps came from it
 * too; a plan that shares none, such as one that replaced it, didn't.
 */
export function planWorkflowId(entries: readonly JournalEntry[]): string | null {
  const plan = currentPlan(entries);
  if (!plan) return null;
  if (plan.workflow_id) return plan.workflow_id;
  const start = entries.findLast((entry) => entry.kind === "plan" && entry.workflow_id);
  if (!start?.workflow_id) return null;
  const startStepIds = new Set(planSteps(start).map((step) => step.id));
  return planSteps(plan).some((step) => startStepIds.has(step.id)) ? start.workflow_id : null;
}

/**
 * Where a step stands. A started step is running only while the session
 * works; a step with no status is ready once every step it depends on is done.
 * A step waits for the user (`needs_input`) when it says so, or while it runs
 * with a question or permission prompt waiting for an answer.
 */
export type StepState =
  "waiting" | "ready" | "running" | "stopped" | "needs_input" | "done" | "failed";

/** One task, a plan step or an item of the agent's own task list, and where it stands. */
export interface StepProgress {
  step: JournalPlanStep;
  state: StepState;
  /** The step's newest status; null until it has one. */
  status: JournalEntry | null;
  /** When the step's current attempt started, in epoch seconds, if it wrote a start. */
  startedAt: number | null;
  /** True for an item of the agent's own task list, which has no history. */
  native: boolean;
  /** What the agent says it's doing while an item of its own list is in progress. */
  activeForm: string | null;
}

/** The start of the attempt that wrote a step's newest status, if it wrote one. */
function attemptStart(statuses: readonly JournalEntry[]): JournalEntry | null {
  for (let index = statuses.length - 1; index >= 0; index -= 1) {
    const status = statusOf(statuses[index]);
    if (status === "started") return statuses[index];
    // An earlier attempt finished, so the newest one wrote no start.
    if (index < statuses.length - 1 && (status === "done" || status === "failed")) return null;
  }
  return null;
}

/**
 * Whether a status restarts its step: a start after the step was done or
 * failed. A start after waiting, a block or an earlier start resumes it.
 */
export function isRestart(entry: JournalEntry, earlier: readonly JournalEntry[]): boolean {
  if (statusOf(entry) !== "started") return false;
  const previous = earlier.findLast((other) => other.step === entry.step && statusOf(other));
  const status = previous ? statusOf(previous) : null;
  return status === "done" || status === "failed";
}

/**
 * Where each step stands, in the given order.
 *
 * @param entries - Loaded entries in position order.
 * @param sessionWorking - Whether the session is working now.
 * @param awaitingUser - Whether a question or permission prompt waits for the user.
 */
export function stepProgress(
  steps: readonly JournalPlanStep[],
  entries: readonly JournalEntry[],
  sessionWorking: boolean,
  awaitingUser = false,
): StepProgress[] {
  const statuses = new Map<string, JournalEntry[]>();
  for (const entry of entries) {
    if (!entry.step || entry.kind !== "status" || !statusOf(entry)) continue;
    const list = statuses.get(entry.step);
    if (list) list.push(entry);
    else statuses.set(entry.step, [entry]);
  }
  const progress: StepProgress[] = steps.map((step) => {
    const history = statuses.get(step.id) ?? [];
    const status = history.at(-1) ?? null;
    const label = status ? statusOf(status) : null;
    let state: StepState = "waiting";
    if (label === "started") {
      state = awaitingUser ? "needs_input" : sessionWorking ? "running" : "stopped";
    } else if (label) state = label;
    return {
      step,
      state,
      status,
      startedAt: attemptStart(history)?.created_at ?? null,
      native: false,
      activeForm: null,
    };
  });
  const done = new Set(
    progress.filter((item) => item.state === "done").map((item) => item.step.id),
  );
  for (const item of progress) {
    if (item.state === "waiting" && item.step.depends_on.every((id) => done.has(id))) {
      item.state = "ready";
    }
  }
  return progress;
}

/** The steps to show when the agent wrote statuses but no plan: one per step it named. */
export function stepsWithoutPlan(entries: readonly JournalEntry[]): JournalPlanStep[] {
  const ids = new Set<string>();
  for (const entry of entries) if (entry.kind === "status" && entry.step) ids.add(entry.step);
  return [...ids].map((id) => ({ id, name: id, goal: null, depends_on: [] }));
}

/**
 * For each step, whether it and the step listed before it can't run at the same
 * time, because one waits on the other, directly or through other steps.
 */
export function linkedToPrevious(steps: readonly JournalPlanStep[]): boolean[] {
  const dependsOn = new Map(steps.map((step) => [step.id, step.depends_on]));
  const waitsOn = (from: string, target: string): boolean => {
    const seen = new Set<string>();
    const pending = [...(dependsOn.get(from) ?? [])];
    for (let id = pending.pop(); id !== undefined; id = pending.pop()) {
      if (id === target) return true;
      if (seen.has(id)) continue;
      seen.add(id);
      pending.push(...(dependsOn.get(id) ?? []));
    }
    return false;
  };
  return steps.map((step, index) => {
    if (index === 0) return false;
    const previous = steps[index - 1].id;
    return waitsOn(step.id, previous) || waitsOn(previous, step.id);
  });
}

/**
 * The tasks that can't start because a task they wait on failed, directly or
 * through tasks that haven't started either: each one's failed tasks by ID,
 * nearest first. A failed task that runs again clears them.
 */
export function failedUpstream(
  progress: readonly StepProgress[],
): ReadonlyMap<string, readonly string[]> {
  const byId = new Map(progress.map((item) => [item.step.id, item]));
  const blocked = new Map<string, string[]>();
  for (const item of progress) {
    if (item.state !== "waiting") continue;
    const failed: string[] = [];
    const seen = new Set<string>();
    const pending = [...item.step.depends_on];
    for (let id = pending.shift(); id !== undefined; id = pending.shift()) {
      if (seen.has(id)) continue;
      seen.add(id);
      const upstream = byId.get(id);
      if (upstream?.state === "failed") failed.push(id);
      // A task that hasn't started passes on whatever keeps it from starting.
      else if (upstream?.state === "waiting") pending.push(...upstream.step.depends_on);
    }
    if (failed.length > 0) blocked.set(item.step.id, failed);
  }
  return blocked;
}

/** How many steps are in each state. */
export function planTally(progress: readonly StepProgress[]): Record<StepState, number> {
  const tally: Record<StepState, number> = {
    waiting: 0,
    ready: 0,
    running: 0,
    stopped: 0,
    needs_input: 0,
    done: 0,
    failed: 0,
  };
  for (const item of progress) tally[item.state] += 1;
  return tally;
}

/** How many tasks are finished: done or failed, since a failed task's work is over too. */
export function finishedTaskCount(progress: readonly StepProgress[]): number {
  return progress.filter((item) => item.state === "done" || item.state === "failed").length;
}

/** One item of the agent's own task list, as its harness reports it (e.g. Claude's tasks). */
export interface NativeTask {
  /** The harness's own ID for the task (Claude's task IDs); absent for Codex's plan. */
  id?: string;
  content: string;
  status: "pending" | "in_progress" | "completed";
  activeForm: string;
}

/**
 * Where each item of the agent's own task list stands. An item in progress is
 * running only while the session works; items have no dependencies or history.
 */
export function nativeTaskProgress(
  tasks: readonly NativeTask[],
  sessionWorking: boolean,
  awaitingUser = false,
): StepProgress[] {
  return tasks.map((task, index) => {
    let state: StepState = "waiting";
    if (task.status === "completed") state = "done";
    else if (task.status === "in_progress") {
      state = awaitingUser ? "needs_input" : sessionWorking ? "running" : "stopped";
    }
    return {
      step: { id: task.id ?? `task-${index + 1}`, name: task.content, goal: null, depends_on: [] },
      state,
      status: null,
      startedAt: null,
      native: true,
      activeForm:
        task.status === "in_progress" && task.activeForm && task.activeForm !== task.content
          ? task.activeForm
          : null,
    };
  });
}

/** A session's tasks: its plan's steps when it has a plan, otherwise the agent's own list. */
export interface SessionTaskList {
  /** The plan the tasks come from; null for the agent's own list. */
  plan: JournalEntry | null;
  progress: StepProgress[];
  /** True when the tasks are the agent's own list. */
  native: boolean;
}

/**
 * The tasks to show for a session. A plan, or statuses written without one,
 * win over the agent's own list, so a workflow never shows two lists.
 *
 * @param entries - Loaded journal entries in position order.
 * @param nativeTasks - The agent's own task list.
 */
export function sessionTaskList(
  entries: readonly JournalEntry[],
  nativeTasks: readonly NativeTask[],
  sessionWorking: boolean,
  awaitingUser = false,
): SessionTaskList {
  const plan = currentPlan(entries);
  const steps = plan ? planSteps(plan) : stepsWithoutPlan(entries);
  if (steps.length > 0) {
    const progress = stepProgress(steps, entries, sessionWorking, awaitingUser);
    return { plan, progress, native: false };
  }
  const progress = nativeTaskProgress(nativeTasks, sessionWorking, awaitingUser);
  return { plan: null, progress, native: true };
}

/** Why an idle list isn't moving: see {@link pausedLabel}. */
export type PausedLabel = "Stopped" | "Run ended";

/**
 * What an idle list says when it has tasks the agent could run and none waits
 * for the user, so it never looks about to run: "Run ended" when an automation
 * run wrote the newest entry, else "Stopped". Null while the session works.
 */
export function pausedLabel(
  progress: readonly StepProgress[],
  entries: readonly JournalEntry[],
  sessionWorking: boolean,
): PausedLabel | null {
  if (sessionWorking || progress.some((item) => item.state === "needs_input")) return null;
  const runnable = progress.some(
    (item) =>
      item.state === "ready" ||
      item.state === "stopped" ||
      (item.native && item.state === "waiting"),
  );
  if (!runnable) return null;
  return entries.at(-1)?.scheduled_task_run_id ? "Run ended" : "Stopped";
}

/**
 * A few words on where the tasks stand, for the chat's one-line preview: a task
 * waiting for the user, else the task in progress, else the next one, else how
 * the list ended ("All done", or "Finished, 1 failed").
 *
 * @param paused - What an idle list says, from {@link pausedLabel}.
 */
export function taskPreviewText(
  progress: readonly StepProgress[],
  paused: PausedLabel | null = null,
): string | null {
  if (progress.length === 0) return null;
  const order: StepState[] = ["needs_input", "running", "stopped", "ready", "waiting"];
  for (const state of order) {
    const item = progress.find((candidate) => candidate.state === state);
    if (!item) continue;
    if (state === "needs_input") return `${WAITING_FOR_YOU}: ${item.step.name}`;
    if (state === "running") return item.activeForm ?? item.step.name;
    if (state === "stopped") return `Stopped: ${item.step.name}`;
    if (state === "waiting") {
      // Nothing else can run, so name the failure that holds the rest up.
      const failedId = failedUpstream(progress).get(item.step.id)?.[0];
      const failed = progress.find((candidate) => candidate.step.id === failedId);
      if (failed) return `Failed: ${failed.step.name}`;
    }
    return paused ? `${paused} · Next: ${item.step.name}` : `Next: ${item.step.name}`;
  }
  const failed = progress.filter((item) => item.state === "failed").length;
  return failed === 0 ? "All done" : `Finished, ${failed} failed`;
}
