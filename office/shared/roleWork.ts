// A one-line summary of what an SDLC role is doing right now, for the Team tab.

import type { AgentRoleId } from "./sdlcTheme.ts";
import { type ActivityItem, toActivity } from "./sessionActivity.ts";
import type { TaskStatus } from "./workflowTypes.ts";

export interface RoleWorkLine {
  role: AgentRoleId;
  sessionId: string | null;
  /** Where the line came from, for styling: live work, an office task, or the last thing said. */
  kind: "todo" | "tool" | "message" | "error" | "task" | "idle" | "not_started";
  text: string;
  /** Epoch seconds of the activity the line reports, when known. */
  at: number | null;
}

export interface RoleWorkInput {
  role: AgentRoleId;
  sessionId: string | null;
  /** The session is mid-turn (running, launching, or waiting). */
  busy: boolean;
  todos: { content: string; status: string; activeForm?: string }[];
  /** Newest first, as Omnigent lists them. */
  items: ActivityItem[];
  /** Open office tasks the role is responsible for, most recently updated first. */
  tasks: { title: string; status: TaskStatus }[];
}

const MAX_LINE = 160;

const TASK_PHRASES: Partial<Record<TaskStatus, string>> = {
  in_progress: "On task",
  blocked: "Blocked on",
  pending: "Assigned",
  submitted: "Submitted for review",
  awaiting_gate: "Waiting for your gate on",
};

// Most pressing first: a blocked task outranks one waiting on review.
const TASK_ORDER: TaskStatus[] = ["blocked", "in_progress", "awaiting_gate", "submitted", "pending"];

function oneLine(text: string): string {
  const line = text.replace(/\s+/g, " ").trim();
  return line.length > MAX_LINE ? `${line.slice(0, MAX_LINE - 1)}…` : line;
}

function taskLine(tasks: RoleWorkInput["tasks"]): string | null {
  const task = TASK_ORDER.map((status) => tasks.find((entry) => entry.status === status)).find(Boolean);
  return task ? `${TASK_PHRASES[task.status]}: ${task.title}` : null;
}

export function roleWorkLine(input: RoleWorkInput): RoleWorkLine {
  const line = (kind: RoleWorkLine["kind"], text: string, at: number | null = null): RoleWorkLine => ({
    role: input.role,
    sessionId: input.sessionId,
    kind,
    text: oneLine(text),
    at,
  });
  const task = taskLine(input.tasks);
  if (!input.sessionId) return task ? line("task", `${task} (starts when the PM dispatches it)`) : line("not_started", "Not started");

  // Relayed office messages and the PO's own words are not the role's work.
  const latest = toActivity(input.items).find((entry) => entry.kind === "agent" || entry.kind === "tool" || entry.kind === "error");
  if (input.busy) {
    const todo = input.todos.find((entry) => entry.status === "in_progress");
    if (todo) return line("todo", todo.activeForm ?? todo.content);
    if (latest?.kind === "tool") return line("tool", latest.text.replace(/^Used tool/, "Using"), latest.at ?? null);
    if (latest?.kind === "agent") return line("message", latest.text, latest.at ?? null);
    return line("task", task ?? "Working");
  }
  if (latest?.kind === "error") return line("error", `Error: ${latest.text}`, latest.at ?? null);
  if (task) return line("task", task);
  if (latest?.kind === "agent") return line("message", `Last said: ${latest.text}`, latest.at ?? null);
  return line("idle", "Idle, no open tasks");
}
