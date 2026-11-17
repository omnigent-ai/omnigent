import type { Conversation } from "@/hooks/useConversations";
import { sessionFamilyGroups } from "@/lib/sessionFamilies";
import { filterConversations, STATUS_BUCKETS, type StatusBucket } from "@/shell/sidebarNav";

export interface KanbanCard {
  /** The family's head row, or the lone session. */
  conversation: Conversation;
  /** Every session the card stands for, head first. */
  members: Conversation[];
  column: StatusBucket;
}

/** The board's left-to-right lanes, so a finished turn moves right into Unread beside Done. */
export const KANBAN_COLUMNS: readonly StatusBucket[] = [
  "Draft",
  "Needs attention",
  "Working",
  "Unread",
  "Done",
];

/**
 * One card per family (or lone session) in the sidebar's Status-grouping columns,
 * keeping the rows' order. A family sits in its most urgent member's column.
 */
export function kanbanColumns(
  rows: readonly Conversation[],
  columnOf: (row: Conversation) => StatusBucket,
): Map<StatusBucket, KanbanCard[]> {
  const columns = new Map<StatusBucket, KanbanCard[]>(KANBAN_COLUMNS.map((column) => [column, []]));
  const families = sessionFamilyGroups(rows);
  const placed = new Set<string>();
  for (const row of rows) {
    if (placed.has(row.id)) continue;
    const members = row.family ? families.get(row.family.id)! : [row];
    for (const member of members) placed.add(member.id);
    const column =
      STATUS_BUCKETS[Math.min(...members.map((m) => STATUS_BUCKETS.indexOf(columnOf(m))))];
    columns.get(column)!.push({ conversation: members[0], members, column });
  }
  return columns;
}

/** Cards with any session matching the search, by the sidebar search's title-or-id rule. */
export function filterKanbanCards(cards: readonly KanbanCard[], searchQuery: string): KanbanCard[] {
  return cards.filter((card) => filterConversations(card.members, searchQuery).length > 0);
}

/** How recently a session must have been created to count as a new arrival. */
export const KANBAN_ARRIVAL_WINDOW_S = 60;

/**
 * Ids of rows that just joined the board, adding them to `known`. Older rows
 * revealed by paging, or seen before, don't count.
 *
 * :param rows: The board's current rows.
 * :param known: Ids already on the board; updated in place.
 * :param nowS: The current time, in epoch seconds.
 * :returns: The newly arrived ids, in row order.
 */
export function takeArrivals(
  rows: readonly Conversation[],
  known: Set<string>,
  nowS: number,
): string[] {
  const arrivals: string[] = [];
  for (const row of rows) {
    if (known.has(row.id)) continue;
    known.add(row.id);
    if (nowS - row.created_at <= KANBAN_ARRIVAL_WINDOW_S) arrivals.push(row.id);
  }
  return arrivals;
}

export type KanbanDropAction = "mark_read" | "mark_unread";

/**
 * What dropping a card on a column does; null snaps it back. Columns follow live
 * session status, so only the read state can move a card: Unread ⇄ Done.
 */
export function kanbanDropAction(card: KanbanCard, target: StatusBucket): KanbanDropAction | null {
  if (card.column === "Unread" && target === "Done") return "mark_read";
  if (card.column === "Done" && target === "Unread") return "mark_unread";
  return null;
}
