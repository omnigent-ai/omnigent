import { useConversationEntryValue } from "@/hooks/useConversationEntryState";
import { useOptionalLoadedConversations } from "@/hooks/useSidebarData";

/** Activity for task progress, including the sidebar's child/background rollup. */
export function useSessionWorking(sessionId: string | null, fallbackWorking = false): boolean {
  const liveWorking = useConversationEntryValue(
    sessionId,
    (state) =>
      state.sessionStatus === "running" ||
      state.sessionStatus === "waiting" ||
      (state.backgroundTaskCount ?? 0) > 0,
  );
  const loaded = useOptionalLoadedConversations();
  const row = loaded?.pages
    .flatMap((page) => page.data)
    .find((conversation) => conversation.id === sessionId);
  // Live list updates arrive between run-list refreshes. An older snapshot
  // must not keep work spinning after the sidebar settles.
  const listedWorking = row
    ? row.status === "running" || row.background_work_running === true
    : fallbackWorking;
  return liveWorking || listedWorking;
}
