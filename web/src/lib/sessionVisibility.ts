import type { Conversation } from "@/hooks/useConversations";

/** Infer ownership independently of the server's visibility query parameter. */
export function sessionVisibility(
  row: Pick<Conversation, "owner" | "permission_level">,
  viewerId: string | null,
): "mine" | "shared" {
  // Admin permissions do not change who owns a session.
  if (viewerId !== null && row.owner) return row.owner === viewerId ? "mine" : "shared";
  if (row.permission_level != null && row.permission_level > 0 && row.permission_level < 4)
    return "shared";
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
