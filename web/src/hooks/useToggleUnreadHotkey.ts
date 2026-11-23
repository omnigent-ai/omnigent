import { showToast } from "@/components/ui/toast";
import type { Conversation } from "@/hooks/useConversations";
import { useCurrentSessionShortcut } from "@/hooks/useCurrentSessionShortcut";
import {
  isExplicitlyUnread,
  markConversationRead,
  markConversationUnread,
} from "@/hooks/useUnseenConversations";
import { isMacPlatform } from "@/lib/hotkeys";
import { canFlagUnread } from "@/lib/sessionMute";

/**
 * Cmd/Ctrl+Alt+U marks the open session's sidebar row unread, or read again
 * if it is already marked, and confirms with a toast. Inert without a row.
 * A muted session can be marked read but not unread.
 */
export function useToggleUnreadHotkey(
  conversation:
    (Pick<Conversation, "id" | "updated_at"> & Partial<Pick<Conversation, "labels">>) | null,
  isMac = isMacPlatform(),
): void {
  useCurrentSessionShortcut(
    "session.toggleUnread",
    () => {
      if (!conversation) return;
      const { id, updated_at: updatedAt } = conversation;
      if (isExplicitlyUnread(id)) {
        markConversationRead(id, updatedAt);
        showToast("Marked as read");
      } else if (!canFlagUnread(conversation)) {
        showToast("Unmute the session to mark it as unread.");
      } else {
        markConversationUnread(id, updatedAt);
        showToast("Marked as unread");
      }
    },
    conversation !== null,
    isMac,
  );
}
