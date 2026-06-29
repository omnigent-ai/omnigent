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
  | { kind: "unseen" };

export function getSessionState(
  conversation: Pick<Conversation, "status" | "pending_elicitations_count"> | undefined | null,
): SessionState | null {
  const pending = conversation?.pending_elicitations_count ?? 0;
  if (pending > 0) return { kind: "awaiting", count: pending };
  if (conversation?.status === "running") return { kind: "running" };
  return null;
}
