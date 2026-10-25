// Durable office state (presentation and workflow), kept in one JSON file
// that survives browser closure and service restarts.

import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import type { Priority } from "../shared/types.ts";
import { emptyTroubleshootState, type TroubleshootState } from "../shared/troubleshoot.ts";
import { emptyWorkflowState, type WorkflowState } from "../shared/workflowTypes.ts";
import type { SessionScan } from "./activity.ts";

export interface RoomState {
  priority: Priority;
  keepUpstairs: boolean;
  /** Persisted once; reconnects and restarts never reset it. */
  firstSyncAt: number;
}

export interface ApprovalDecision {
  projectId: string | null;
  sessionId: string;
  elicitationId: string;
  action: "accept" | "decline";
  decidedAt: number;
}

export interface OfficeState {
  version: 1;
  rooms: Record<string, RoomState>;
  sessions: Record<string, SessionScan>;
  /** Keyed by `${sessionId}:${elicitationId}`, so a retried decision is recorded once. */
  decisions: Record<string, ApprovalDecision>;
  workflow: WorkflowState;
  troubleshooting: TroubleshootState;
}

export function emptyState(): OfficeState {
  return { version: 1, rooms: {}, sessions: {}, decisions: {}, workflow: emptyWorkflowState(), troubleshooting: emptyTroubleshootState() };
}

export class StateStore {
  readonly path: string;
  state: OfficeState;

  constructor(path: string) {
    this.path = path;
    this.state = StateStore.load(path);
  }

  private static load(path: string): OfficeState {
    try {
      const parsed = JSON.parse(readFileSync(path, "utf8")) as Partial<OfficeState>;
      return {
        ...emptyState(),
        ...parsed,
        workflow: { ...emptyWorkflowState(), ...parsed.workflow },
        troubleshooting: { ...emptyTroubleshootState(), ...parsed.troubleshooting },
        version: 1,
      };
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return emptyState();
      throw new Error(`Cannot read office state at ${path}: ${(error as Error).message}`);
    }
  }

  /** Write atomically so a crash mid-write never corrupts the file. */
  save(): void {
    mkdirSync(dirname(this.path), { recursive: true });
    const temporaryPath = `${this.path}.tmp`;
    writeFileSync(temporaryPath, JSON.stringify(this.state, null, 2), { mode: 0o600 });
    renameSync(temporaryPath, this.path);
  }

  ensureRoom(projectId: string, now: number): RoomState {
    let room = this.state.rooms[projectId];
    if (!room) {
      room = { priority: "normal", keepUpstairs: false, firstSyncAt: now };
      this.state.rooms[projectId] = room;
    }
    return room;
  }
}
