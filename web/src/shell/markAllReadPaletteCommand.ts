// With quick_pick_palette on, ⌘K offers "Mark all as read" for every loaded session showing an
// unread dot, marking each read as the bulk bar's Mark as read does.

import { MailOpenIcon } from "lucide-react";
import { useMemo, useRef } from "react";

import type { Conversation } from "@/hooks/useConversations";
import { useActiveRootSessionId } from "@/hooks/useSession";
import { useLoadedConversations } from "@/hooks/useSidebarData";
import {
  isConversationUnseen,
  isExplicitlyUnread,
  markConversationRead,
  useUnseenTick,
} from "@/hooks/useUnseenConversations";
import { useParams } from "@/lib/routing";
import { showsUnreadFor } from "@/lib/sessionMute";
import { useQuickPickPaletteCommands } from "@/shell/screenCommands/useQuickPickPaletteCommands";
import type { PaletteCommand } from "@/store/paletteCommands";

/** A session to mark read, at the version its dot is for. */
export interface UnreadSessionMark {
  conversationId: string;
  updatedAt: number;
}

/** The read state a row's dot reads, injected so the rule stays pure. */
export interface RowReadState {
  isUnseen: (id: string, updatedAt: number, status: string | undefined) => boolean;
  isExplicitlyUnread: (id: string) => boolean;
  showsUnread: (row: Conversation, unseen: boolean) => boolean;
}

/**
 * One mark per loaded row showing an unread dot: unseen, not muted, not archived, and not an
 * open row (`openConversationIds`) unless the user marked it unread.
 */
export function unreadSessionMarks(
  rows: readonly Conversation[],
  openConversationIds: readonly string[],
  readState: RowReadState,
): UnreadSessionMark[] {
  const marks = new Map<string, UnreadSessionMark>();
  for (const row of rows) {
    if (row.archived || marks.has(row.id)) continue;
    if (!readState.showsUnread(row, readState.isUnseen(row.id, row.updated_at, row.status))) {
      continue;
    }
    if (openConversationIds.includes(row.id) && !readState.isExplicitlyUnread(row.id)) continue;
    marks.set(row.id, { conversationId: row.id, updatedAt: row.updated_at });
  }
  return [...marks.values()];
}

/** The command's label for `unread` sessions. */
export function markAllReadLabel(unread: number): string {
  return unread === 1
    ? "Mark 1 unread session as read"
    : `Mark all ${unread.toLocaleString()} unread sessions as read`;
}

/** The ⌘K command for `count` unread sessions; none while nothing is unread. */
export function markAllReadCommands(count: number, run: () => void): PaletteCommand[] {
  if (count === 0) return [];
  return [
    {
      id: "sessions.mark-all-read",
      label: markAllReadLabel(count),
      icon: MailOpenIcon,
      keywords: ["mark all as read", "clear unread", "seen", "notifications", "badge"],
      run,
    },
  ];
}

const ROW_READ_STATE: RowReadState = {
  isUnseen: isConversationUnseen,
  isExplicitlyUnread,
  showsUnread: showsUnreadFor,
};

/** Renders nothing; registers "Mark all as read" while a loaded session is unread. */
export function MarkAllReadPaletteCommand(): null {
  const { data } = useLoadedConversations();
  const { conversationId } = useParams<{ conversationId: string }>();
  // A nested session on screen opens its family's root row, which hides its dot as the sidebar does.
  const activeRootId = useActiveRootSessionId(conversationId ?? null);
  // Recount the moment a dot changes, not on the next list poll.
  const unseenTick = useUnseenTick();
  const marks = useMemo(
    () =>
      unreadSessionMarks(
        data?.pages.flatMap((page) => page.data) ?? [],
        [conversationId, activeRootId].filter((id): id is string => id != null),
        ROW_READ_STATE,
      ),
    // `unseenTick` stands for the read-state store the predicates read.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [data, conversationId, activeRootId, unseenTick],
  );
  const latestMarks = useRef(marks);
  latestMarks.current = marks;
  const commands = useMemo(
    () =>
      markAllReadCommands(marks.length, () => {
        for (const mark of latestMarks.current) {
          markConversationRead(mark.conversationId, mark.updatedAt);
        }
      }),
    [marks.length],
  );
  useQuickPickPaletteCommands("mark-all-read", "Sessions", commands);
  return null;
}
