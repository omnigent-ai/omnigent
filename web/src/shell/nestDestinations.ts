// The nests a session can move into, shared by the sidebar's Move session → Nests menu and
// ⌘K's nest picker, so both offer the same choices for the same search.

import type { Conversation } from "@/hooks/useConversations";

import { conversationDisplayLabel, isOwnedByViewer } from "./sidebarNav";

/** A nest's name: its representative's title, as the sidebar's nest row shows it. */
export function nestLabel(row: Conversation): string {
  return row.family?.representative_title ?? conversationDisplayLabel(row);
}

/**
 * One row per nest the viewer owns, other than the session's own. Loaded rows match
 * `search` by nest name. The server's search hits are kept as they are: they matched a
 * member's title, which can differ from the nest's name.
 */
export function nestDestinations({
  conversation,
  loaded,
  searchHits,
  search,
  viewerId,
}: {
  conversation: Conversation;
  loaded: readonly Conversation[];
  /** Title-search results for `search`, or none while they're for an older query. */
  searchHits: readonly Conversation[];
  search: string;
  viewerId: string | null;
}): Conversation[] {
  const needle = search.trim().toLowerCase();
  const candidates = [
    ...loaded.filter((row) => nestLabel(row).toLowerCase().includes(needle)),
    ...searchHits,
  ].filter(
    (row) =>
      row.family &&
      row.family.id !== conversation.family?.id &&
      row.id !== conversation.id &&
      !row.archived &&
      isOwnedByViewer(row, viewerId),
  );
  return [...new Map(candidates.map((row) => [row.family!.id, row])).values()];
}
