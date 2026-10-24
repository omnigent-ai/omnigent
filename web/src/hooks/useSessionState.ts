// Per-row state derivation for the sidebar badge.
// Priority: awaiting > running > starting > disconnected > error > unseen >
// asleep > no badge. (`starting` and `unseen` are folded in by the sidebar
// row, which owns the chat-store and read-state inputs.)
//
// `asleep` appears only when the caller passes `liveness` and the runner is
// explicitly stopped (`runnerOnline === false`) on a live host (the same
// `classifyRunnerLiveness` the composer uses): the next message restarts it.
// `undefined` (not yet observed) renders as before. A host that is down gets
// no sidebar badge: whether it wakes on send (`host_resumable`) is not on the
// row, so the chat view owns that distinction. Liveness never overrides
// awaiting or running, and `asleep` is lowest so unread replies show first.
//
// Errors are independent of read state. Native sessions can settle to idle
// after an error, so callers also inspect the latest conversation message.

import type { Conversation } from "@/hooks/useConversations";
import { classifyRunnerLiveness } from "@/hooks/useSessionLiveness";
import type { LatestSessionError } from "@/lib/sessionError";

/** Runner/host reachability signals for a row (see `RunnerHealthProvider`). */
export interface RowLiveness {
  runnerOnline: boolean | undefined;
  hostOnline: boolean | null | undefined;
  /** Viewer cannot send, so the next-message restart promise does not apply. */
  readOnly?: boolean;
}

export type SessionState =
  | { kind: "awaiting"; count: number }
  | { kind: "running" }
  | { kind: "error" }
  | { kind: "disconnected" }
  // Runner stopped (idle timeout); the next message restarts it unless `readOnly`.
  | { kind: "asleep"; readOnly?: boolean }
  | { kind: "unseen" }
  // The open session's launch/relaunch window — a send in flight or the PTY
  // being created before the server confirms `running`. Not derivable from a
  // conversation row (it reads the chat store), so `getSessionState` never
  // returns it; the sidebar row folds it in for the bound session only.
  | { kind: "starting" };

export function getSessionState(
  conversation: Pick<Conversation, "status" | "pending_elicitations_count"> | undefined | null,
  latestError: LatestSessionError | null = null,
  liveness?: RowLiveness,
): SessionState | null {
  const pending = conversation?.pending_elicitations_count ?? 0;
  if (pending > 0) return { kind: "awaiting", count: pending };
  if (conversation?.status === "running") return { kind: "running" };
  if (latestError === "disconnected") return { kind: "disconnected" };
  if (latestError === "recovered_disconnect") return asleepOrNull(liveness);
  if (conversation?.status === "failed" || latestError === "error") return { kind: "error" };
  return asleepOrNull(liveness);
}

function asleepOrNull(liveness: RowLiveness | undefined): SessionState | null {
  if (liveness?.runnerOnline !== false) return null;
  const reach = classifyRunnerLiveness({
    runnerOnline: liveness.runnerOnline,
    hostOnline: liveness.hostOnline,
  });
  if (reach !== "runner_asleep") return null;
  return liveness.readOnly ? { kind: "asleep", readOnly: true } : { kind: "asleep" };
}
