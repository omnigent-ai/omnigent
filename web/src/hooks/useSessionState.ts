// Per-row state derivation for the sidebar badge.
// Priority: awaiting > running > no badge.
//
// Liveness (runner / host reachability) is no longer a sidebar state:
// it surfaces in the open-session view (see `useSessionLiveness`), so the
// sidebar no longer renders a "disconnected" badge and `getSessionState`
// no longer reads runner liveness.
//
// "failed" is intentionally not a sidebar state either — the chat surface
// is the right place to read what failed. Conflating it into the same red
// badge also led to a stale-cache bug where a prior turn's
// `_session_status_cache["failed"]` would mask a fresh elicitation.

import type { Conversation } from "@/hooks/useConversations";

export type SessionState =
  | { kind: "awaiting"; count: number }
  | { kind: "running" }
  | { kind: "unseen" }
  // The open session's launch/relaunch window — a send in flight or the PTY
  // being created before the server confirms `running`. Not derivable from a
  // conversation row (it reads the chat store), so `getSessionState` never
  // returns it; the sidebar row folds it in for the bound session only.
  | { kind: "starting" };

export function getSessionState(
  conversation: Pick<Conversation, "status" | "pending_elicitations_count"> | undefined | null,
): SessionState | null {
  const pending = conversation?.pending_elicitations_count ?? 0;
  if (pending > 0) return { kind: "awaiting", count: pending };
  if (conversation?.status === "running") return { kind: "running" };
  return null;
}

/** The sidebar's status filter — mirrors the server's `GET /v1/sessions?status=`
 * vocabulary (see `omnigent/server/routes/sessions/routes_core.py`). */
export type SessionStatusFilter = "all" | "active" | "completed";

/** Mirrors the server's `omnigent.closed` label (see
 * `omnigent.session_lifecycle.CLOSED_LABEL_KEY`); the server always
 * synthesizes this label onto every session response, including the legacy
 * title-marker rows, so the client only ever needs to read the label. */
const CLOSED_LABEL_KEY = "omnigent.closed";

/**
 * Whether a session belongs in the given status bucket.
 *
 * "active" = an agent is currently running/awaiting a response on it (same
 * signal as {@link getSessionState}) and it isn't closed. "completed" is
 * everything else — idle/failed/never-observed, or any closed session
 * regardless of its live status.
 *
 * @param conversation - The session row to classify.
 * @param filter - The sidebar's current status filter.
 */
export function matchesSessionStatusFilter(
  conversation: Pick<Conversation, "status" | "pending_elicitations_count" | "labels">,
  filter: SessionStatusFilter,
): boolean {
  if (filter === "all") return true;
  const closed = conversation.labels?.[CLOSED_LABEL_KEY] === "true";
  const active = !closed && getSessionState(conversation) !== null;
  return filter === "active" ? active : !active;
}
