import type { Conversation } from "@/hooks/useConversations";

/** Infer ownership independently of the server's visibility query parameter. */
export function sessionVisibility(
  row: Pick<Conversation, "owner" | "permission_level">,
  viewerId: string | null,
): "mine" | "shared" {
  if ((viewerId !== null && row.owner === viewerId) || (row.permission_level ?? 0) >= 4)
    return "mine";
  if (viewerId !== null && row.owner) return "shared";
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
