import type { Conversation } from "@/hooks/useConversations";
import { canFlagUnread } from "@/lib/sessionMute";
import { sessionFamilyGroups } from "@/lib/sessionFamilies";
import {
  dedupeConversationsById,
  filterConversations,
  isSharedAutomationRun,
  sessionInSidebarFolder,
  STATUS_BUCKETS,
  type StatusBucket,
} from "@/shell/sidebarNav";

export interface KanbanCard {
  /** The family's head row, or the lone session. */
  conversation: Conversation;
  /** Every session the card stands for, head first. */
  members: Conversation[];
  column: StatusBucket;
  /** Nested members that put the card in Needs attention when its head doesn't need it. */
  attentionMembers: Conversation[];
}

/**
 * A nested member that failed before the family's latest activity: it keeps
 * its own Failed state but no longer holds the whole family in Needs attention.
 */
function isStaleNestedFailure(member: Conversation, members: readonly Conversation[]): boolean {
  if (member.status !== "failed" || (member.pending_elicitations_count ?? 0) > 0) return false;
  // Status changes do not advance updated_at. Some failures have no recorded
  // timestamp, so keep those visible rather than guessing their age.
  const failedAt = Number(member.labels?.["omnigent.last_task_error_at"]);
  if (!Number.isFinite(failedAt) || failedAt <= 0) return false;
  const latestActivity = Math.max(failedAt, member.updated_at);
  return members.some((other) => other !== member && other.updated_at > latestActivity);
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
 * The board's unarchived sessions, filed as the sidebar folder files them: the
 * viewer's own project sessions and, on the default automation board, other
 * users' runs shared with the viewer from the sidebar's loaded rows.
 */
export function projectBoardRows(
  projectRows: readonly Conversation[],
  loadedRows: readonly Conversation[],
  project: { id: string | null; name: string },
  viewerId: string | null,
): Conversation[] {
  const sharedRuns = loadedRows.filter((row) => isSharedAutomationRun(row, viewerId));
  return dedupeConversationsById([...projectRows, ...sharedRuns]).filter(
    (row) => !row.archived && sessionInSidebarFolder(row, project, viewerId, true),
  );
}

/**
 * One card per family (or lone session) in the sidebar's Status-grouping columns,
 * keeping the rows' order. A family sits in its most urgent member's column,
 * except that a nested failure older than the family's latest activity doesn't count.
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
    const [head, ...nested] = members;
    const headColumn = columnOf(head);
    const counted = nested.flatMap((member) => {
      const bucket = columnOf(member);
      return bucket === "Needs attention" && isStaleNestedFailure(member, members)
        ? []
        : [{ member, bucket }];
    });
    const column =
      STATUS_BUCKETS[
        Math.min(
          STATUS_BUCKETS.indexOf(headColumn),
          ...counted.map(({ bucket }) => STATUS_BUCKETS.indexOf(bucket)),
        )
      ];
    const attentionMembers =
      headColumn === "Needs attention"
        ? []
        : counted.filter(({ bucket }) => bucket === "Needs attention").map(({ member }) => member);
    columns.get(column)!.push({ conversation: head, members, column, attentionMembers });
  }
  return columns;
}

/** The card's note, beside "N nested", on nested sessions that put it in Needs attention. */
export function nestedAttentionLabel(card: KanbanCard): string | null {
  const count = card.attentionMembers.length;
  if (count === 0) return null;
  const onlyFailures = card.attentionMembers.every(
    (member) => member.status === "failed" && (member.pending_elicitations_count ?? 0) === 0,
  );
  return onlyFailures ? `${count} failed` : `${count} need${count === 1 ? "s" : ""} attention`;
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
  if (card.column === "Done" && target === "Unread" && canFlagUnread(card.conversation))
    return "mark_unread";
  return null;
}
