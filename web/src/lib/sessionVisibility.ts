import type { Conversation } from "@/hooks/useConversations";
import { canReadLevel, isOwnerLevel } from "@/lib/permissionsApi";

/** Infer ownership independently of the server's visibility query parameter. */
export function sessionVisibility(
  row: Pick<Conversation, "owner" | "permission_level">,
  viewerId: string | null,
): "mine" | "shared" {
  // Admin permissions do not change who owns a session.
  if (viewerId !== null && row.owner) return row.owner === viewerId ? "mine" : "shared";
  const level = row.permission_level;
  if (level != null && canReadLevel(level) && !isOwnerLevel(level)) return "shared";
  // Older single-user servers omit ownership; unknown rows must not appear as shared.
  return "mine";
}

export function filterSessionScope(
  rows: Conversation[],
  scope: "mine" | "shared",
  viewerId: string | null,
): Conversation[] {
  return rows.filter((row) => !row.archived && sessionVisibility(row, viewerId) === scope);
}
