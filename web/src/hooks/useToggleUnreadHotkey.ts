import { showToast } from "@/components/ui/toast";
import type { Conversation } from "@/hooks/useConversations";
import { useCurrentSessionShortcut } from "@/hooks/useCurrentSessionShortcut";
import {
  isExplicitlyUnread,
  markConversationRead,
  markConversationUnread,
} from "@/hooks/useUnseenConversations";
import { isMacPlatform } from "@/lib/hotkeys";

/**
 * Cmd/Ctrl+Alt+U marks the open session's sidebar row unread, or read again
 * if it is already marked, and confirms with a toast. Inert without a row.
 */
export function useToggleUnreadHotkey(
  conversation: Pick<Conversation, "id" | "updated_at"> | null,
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
      } else {
        markConversationUnread(id, updatedAt);
        showToast("Marked as unread");
      }
    },
    conversation !== null,
    isMac,
  );
}
