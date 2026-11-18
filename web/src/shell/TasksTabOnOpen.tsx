import { useEffect, useRef } from "react";

import { useSessionTasks } from "@/hooks/useSessionTasks";
import { useChatStore } from "@/store/chatStore";
import { showTasksTabBesideChat } from "@/store/sessionTasksActions";

/**
 * Opening a session that has a plan or tasks, from anywhere, shows its Tasks
 * tab once they're known. Only the opening does: a tab picked afterwards stays
 * until the session is opened again. Renders nothing, so journal updates don't
 * re-render the shell.
 */
export function TasksTabOnOpen({
  conversationId,
  skip,
}: {
  conversationId: string | undefined;
  /** The link asked for something else, e.g. a file with `?file=`. */
  skip: boolean;
}) {
  const tasks = useSessionTasks(conversationId, true);
  // The agent's own task list arrives with the session's snapshot.
  const snapshotLoaded = useChatStore(
    (state) => state.conversationId === conversationId && !state.loadingConversation,
  );
  const known = snapshotLoaded && !tasks.loading;
  const hasTasks = tasks.progress.length > 0;
  const openingRef = useRef<string | undefined>(undefined);
  useEffect(() => {
    openingRef.current = conversationId;
  }, [conversationId]);
  useEffect(() => {
    if (conversationId === undefined || openingRef.current !== conversationId || !known) return;
    openingRef.current = undefined;
    if (hasTasks && !skip) showTasksTabBesideChat(conversationId);
  }, [conversationId, known, hasTasks, skip]);
  return null;
}
