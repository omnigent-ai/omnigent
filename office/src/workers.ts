// Honest worker states derived from Omnigent session status and liveness.

import type { SessionItem, WorkerSession } from "./api.ts";

export type WorkerState =
  | "working"
  | "waiting_human"
  | "waiting_dependency"
  | "failed"
  | "offline"
  | "completed";

export const WORKER_STATE_LABELS: Record<WorkerState, string> = {
  working: "Working",
  waiting_human: "Waiting on you",
  waiting_dependency: "Waiting on a dependency",
  failed: "Failed",
  offline: "Offline",
  completed: "Completed",
};

/**
 * A lost host never looks like progress: failure and offline outrank every
 * other signal, then human requests, then live work.
 */
export function deriveWorkerState(session: WorkerSession): WorkerState {
  if (session.status === "failed") return "failed";
  if (session.host_online === false) return "offline";
  if (session.pending_elicitations_count > 0) return "waiting_human";
  if (session.status === "waiting") return "waiting_dependency";
  if (session.status === "running" || session.status === "launching") return "working";
  return "completed";
}

/** A stable appearance index so a worker keeps its look across reloads. */
export function appearanceIndex(key: string, variants: number): number {
  let hash = 0;
  for (const character of key) hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
  return hash % variants;
}

export interface ActivityEntry {
  id: string;
  kind: "you" | "agent" | "office" | "tool" | "error";
  text: string;
  at?: number;
}

const MAX_ENTRY_LENGTH = 400;

function clip(text: string): string {
  return text.length > MAX_ENTRY_LENGTH ? `${text.slice(0, MAX_ENTRY_LENGTH)}…` : text;
}

function messageText(item: SessionItem): string {
  return (item.content ?? [])
    .map((block) => block.text ?? "")
    .join("")
    .trim();
}

/**
 * Visible activity only: messages, tool calls, and errors. Reasoning items
 * are deliberately skipped so the office never implies access to them.
 */
export function toActivity(items: SessionItem[]): ActivityEntry[] {
  const entries: ActivityEntry[] = [];
  for (const item of items) {
    if (item.type === "message" && !item.is_meta) {
      const text = messageText(item);
      const kind = item.role !== "user" ? "agent" : item.office_relayed ? "office" : "you";
      if (text) entries.push({ id: item.id, kind, text: clip(text), at: item.created_at });
    } else if (item.type === "function_call" && item.name) {
      entries.push({ id: item.id, kind: "tool", text: `Used tool ${item.name}`, at: item.created_at });
    } else if (item.type === "error" && item.message) {
      entries.push({ id: item.id, kind: "error", text: clip(item.message), at: item.created_at });
    }
  }
  return entries;
}
