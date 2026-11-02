// Honest worker states derived from Omnigent session status and liveness.

import type { RoleHealth } from "../shared/troubleshoot.ts";
import type { WorkerSession } from "./api.ts";

export type WorkerState =
  | "working"
  | "waiting_human"
  | "waiting_dependency"
  | "needs_help"
  | "failed"
  | "offline"
  | "idle";

export const WORKER_STATE_LABELS: Record<WorkerState, string> = {
  working: "Working",
  waiting_human: "Waiting on you",
  waiting_dependency: "Waiting on a dependency",
  needs_help: "Needs help",
  failed: "Failed",
  offline: "Offline",
  idle: "Idle",
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
  return "idle";
}

/**
 * The one state a room shows for a session, on the canvas, in lists, and in the
 * Team tab: a problem the office diagnosed (and the PO has not dismissed) wins
 * over Omnigent's own status, so a stalled or failed turn never looks fine.
 */
export function roomWorkerState(session: WorkerSession, health: RoleHealth | undefined): WorkerState {
  const raw = deriveWorkerState(session);
  if (raw === "failed" || raw === "offline" || !health?.problem || health.dismissed) return raw;
  return "needs_help";
}

/** A stable appearance index so a worker keeps its look across reloads. */
export function appearanceIndex(key: string, variants: number): number {
  let hash = 0;
  for (const character of key) hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
  return hash % variants;
}
