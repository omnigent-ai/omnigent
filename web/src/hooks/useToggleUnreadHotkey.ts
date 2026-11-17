import { showToast } from "@/components/ui/toast";
import { useCurrentSessionShortcut } from "@/hooks/useCurrentSessionShortcut";
import { toggleConversationUnread } from "@/hooks/useUnseenConversations";
import { isMacPlatform } from "@/lib/hotkeys";

/** The open session whose read state the shortcut and command palette flip. */
export interface ReadStateTarget {
  id: string;
  /** Update time the sidebar row shows, which marking unread stays below. */
  updatedAt: number;
  /** Newest update time known from any source, which marking read covers. */
  latestUpdatedAt?: number;
}

/** Marks the open session unread, or read again if it is already marked, and says which. */
export function toggleOpenSessionUnread(target: ReadStateTarget): void {
  const unread = toggleConversationUnread(target.id, target.updatedAt, target.latestUpdatedAt);
  showToast(unread ? "Marked as unread" : "Marked as read");
}

/** Cmd/Ctrl+Alt+U toggles the open session's unread flag; inert without one. */
export function useToggleUnreadHotkey(
  target: ReadStateTarget | null,
  isMac = isMacPlatform(),
): void {
  useCurrentSessionShortcut(
    "session.toggleUnread",
    () => {
      if (target) toggleOpenSessionUnread(target);
    },
    target !== null,
    isMac,
  );
}
