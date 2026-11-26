import { useCallback } from "react";
import { useIsMutating, useQueryClient } from "@tanstack/react-query";
import { useTogglePinnedConversation, type Conversation } from "@/hooks/useConversations";
import { isIndentedSibling, isSessionPinned } from "@/lib/sessionFamilies";
import { PIN_WRITE_MUTATION_KEY } from "@/lib/sessionListCache";
import { unpinWithUndo } from "@/shell/unpinUndoToast";

/**
 * Tile favorites are pinned sessions, so a star syncs across devices and shows
 * in the sidebar's Pinned section. Unstarring offers the sidebar's Undo toast.
 */
export function useTileFavorite() {
  const queryClient = useQueryClient();
  const togglePinned = useTogglePinnedConversation();
  const saving = useIsMutating({ mutationKey: PIN_WRITE_MUTATION_KEY }) > 0;

  const toggle = useCallback(
    (conversation: Conversation) => {
      if (isSessionPinned(conversation)) {
        unpinWithUndo(queryClient, togglePinned.mutateAsync, conversation.id, conversation);
      } else {
        togglePinned.mutate({ id: conversation.id, pinned: true });
      }
    },
    [queryClient, togglePinned],
  );

  return { toggle, saving };
}

/** Nested family members follow their representative's pin, as in the sidebar. */
export function canFavorite(conversation: Conversation): boolean {
  return !isIndentedSibling(conversation);
}
