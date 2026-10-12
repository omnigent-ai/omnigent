import type { Conversation } from "@/hooks/useConversations";
import { markAllSeen } from "@/hooks/useSeenComments";
import { isConversationUnseen, markConversationRead } from "@/hooks/useUnseenConversations";
import { dismissAllToasts } from "@/components/ui/toast";

/** Clear dismissible notification state while leaving approvals unresolved. */
export function clearAllNotifications(
  conversations: Conversation[],
  unseenCommentIds: string[],
): void {
  for (const conversation of conversations) {
    if (isConversationUnseen(conversation.id, conversation.updated_at, conversation.status)) {
      markConversationRead(conversation.id, conversation.updated_at);
    }
  }
  markAllSeen(unseenCommentIds);
  dismissAllToasts();
}
