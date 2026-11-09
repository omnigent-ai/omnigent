// Chat markers for a session's tasks, as the server writes them into the
// transcript: where a task started, finished, failed, blocked or began waiting
// for the user, and each change to the plan. They render like sub-agent markers
// and link to the Tasks tab.

import { WAITING_FOR_YOU, type StepProgress, type StepStatus } from "@/lib/sessionJournal";

/** A plan-change line's kind: the first plan, a new one, a new name, or any other change. */
export type PlanChange = "made" | "replaced" | "renamed" | "updated";

export type TaskActivity =
  | {
      kind: "task";
      /** The plan step, or the agent's own task (its ID, or `task-<n>` by position). */
      stepId: string;
      name: string;
      status: StepStatus;
      text: string | null;
      /** The journal entry behind a plan task's marker; null for the agent's own list. */
      entryId: string | null;
      native: boolean;
    }
  | {
      kind: "plan";
      entryId: string;
      change: PlanChange;
      name: string;
      tasks: number;
      added: string[];
      removed: string[];
    };

const STATUSES: readonly string[] = ["started", "done", "needs_input", "blocked", "failed"];
const CHANGES: readonly string[] = ["made", "replaced", "renamed", "updated"];
// Past this many names, a plan line counts them instead.
const NAMES_IN_A_LINE = 3;

function strings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((name): name is string => typeof name === "string")
    : [];
}

/** Read a task marker or plan-change line from a persisted resource event. */
export function readTaskActivity(item: Record<string, unknown>): TaskActivity | null {
  if (item.type !== "resource_event" || item.resource_type !== "journal") return null;
  const resource = item.resource;
  if (!resource || typeof resource !== "object") return null;
  const fields = resource as Record<string, unknown>;
  const name = typeof fields.name === "string" ? fields.name : "";
  const entryId = typeof fields.entry_id === "string" && fields.entry_id ? fields.entry_id : null;
  if (item.event_type === "session.journal.task") {
    const status = fields.status;
    if (typeof status !== "string" || !STATUSES.includes(status)) return null;
    if (typeof item.resource_id !== "string" || !item.resource_id) return null;
    return {
      kind: "task",
      stepId: item.resource_id,
      name: name || item.resource_id,
      status: status as StepStatus,
      text: typeof fields.text === "string" && fields.text ? fields.text : null,
      entryId,
      native: fields.native === true,
    };
  }
  if (item.event_type === "session.journal.plan") {
    const change = fields.change;
    if (typeof change !== "string" || !CHANGES.includes(change) || !entryId) return null;
    return {
      kind: "plan",
      entryId,
      change: change as PlanChange,
      name,
      tasks: typeof fields.tasks === "number" ? fields.tasks : 0,
      added: strings(fields.added),
      removed: strings(fields.removed),
    };
  }
  return null;
}

/** How a task marker leads: "Started", "Completed", … */
export function taskMarkerLabel(status: StepStatus): string {
  return {
    started: "Started",
    done: "Completed",
    failed: "Failed",
    blocked: "Blocked",
    needs_input: `${WAITING_FOR_YOU} on`,
  }[status];
}

function changedTasks(names: string[], verb: string): string {
  if (names.length === 0) return "";
  return names.length > NAMES_IN_A_LINE
    ? `${verb} ${names.length} tasks`
    : `${verb} ${names.join(", ")}`;
}

/** A plan-change line: "Made a plan: Ship it · 5 tasks", "Updated the plan: added A, removed B", … */
export function planChangeText(activity: Extract<TaskActivity, { kind: "plan" }>): string {
  const count = `${activity.tasks} ${activity.tasks === 1 ? "task" : "tasks"}`;
  switch (activity.change) {
    case "made":
      return `Made a plan: ${activity.name} · ${count}`;
    case "replaced":
      return `Replaced the plan with ${activity.name} · ${count}`;
    case "renamed":
      return `Renamed the plan to ${activity.name}`;
    case "updated": {
      const changes = [
        changedTasks(activity.added, "added"),
        changedTasks(activity.removed, "removed"),
      ].filter(Boolean);
      return changes.length > 0 ? `Updated the plan: ${changes.join(", ")}` : "Updated the plan";
    }
  }
}

/**
 * The ID a marker answers `?message=` deep links to, which the Tasks tab can
 * name without knowing the marker's transcript item: its journal entry, or for
 * the agent's own list (which has no entries) its task and status.
 */
export function taskMarkerId(activity: TaskActivity): string {
  if (activity.entryId) return journalMarkerId(activity.entryId);
  // Only the agent's own tasks have no entry, and a plan line always has one.
  return activity.kind === "task" ? todoMarkerId(activity.stepId, activity.status) : "";
}

/** The marker ID for a journal entry: a plan task's status or a plan change. */
export function journalMarkerId(entryId: string): string {
  return `journal-${entryId}`;
}

/** The marker ID for one of the agent's own tasks reaching a status. */
export function todoMarkerId(taskId: string, status: string): string {
  return `todo-${taskId}-${status}`;
}

/**
 * The chat marker a task in the Tasks tab links to: its newest start while it
 * runs or is stopped, else the one where it ended. Null before it starts.
 */
export function taskMarkerTarget(item: StepProgress): string | null {
  if (!item.native) return item.status ? journalMarkerId(item.status.id) : null;
  if (item.state === "done") return todoMarkerId(item.step.id, "done");
  if (item.state === "running" || item.state === "stopped" || item.state === "needs_input") {
    return todoMarkerId(item.step.id, "started");
  }
  return null;
}
