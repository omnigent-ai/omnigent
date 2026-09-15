// Per-row state derivation for the sidebar badge.
// Priority: awaiting > running > error > no badge.
//
// Liveness (runner / host reachability) is no longer a sidebar state:
// it surfaces in the open-session view (see `useSessionLiveness`), so the
// sidebar no longer renders a "disconnected" badge and `getSessionState`
// no longer reads runner liveness.
//
// Errors are independent of read state. Native sessions can settle to idle
// after an error, so callers also inspect the latest conversation message.

import type { Conversation } from "@/hooks/useConversations";

export type SessionState =
  | { kind: "awaiting"; count: number }
  | { kind: "running" }
  | { kind: "error" }
  | { kind: "unseen" }
  // The open session's launch/relaunch window — a send in flight or the PTY
  // being created before the server confirms `running`. Not derivable from a
  // conversation row (it reads the chat store), so `getSessionState` never
  // returns it; the sidebar row folds it in for the bound session only.
  | { kind: "starting" };

export function getSessionState(
  conversation: Pick<Conversation, "status" | "pending_elicitations_count"> | undefined | null,
  latestMessageIsError = false,
): SessionState | null {
  const pending = conversation?.pending_elicitations_count ?? 0;
  if (pending > 0) return { kind: "awaiting", count: pending };
  if (conversation?.status === "running") return { kind: "running" };
  if (conversation?.status === "failed" || latestMessageIsError) return { kind: "error" };
  return null;
}
