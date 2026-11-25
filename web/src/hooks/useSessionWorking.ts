import { useConversationEntryValue } from "@/hooks/useConversationEntryState";
import { useLoadedConversation } from "@/hooks/useSidebarData";

/**
 * Activity for task progress, including the sidebar's child/background rollup and,
 * for a session streamed here, a sandbox launch it waits on.
 */
export function useSessionWorking(sessionId: string | null, fallbackWorking = false): boolean {
  const liveWorking = useConversationEntryValue(
    sessionId,
    (state) =>
      state.sessionStatus === "running" ||
      state.sessionStatus === "waiting" ||
      (state.backgroundTaskCount ?? 0) > 0 ||
      (state.sandboxStatus !== null && state.sandboxStatus.stage !== "failed"),
  );
  // Only this session's row, so other sessions' turn edges skip the caller.
  const row = useLoadedConversation(sessionId);
  // Live list updates arrive between run-list refreshes. An older snapshot
  // must not keep work spinning after the sidebar settles.
  const listedWorking = row
    ? row.status === "running" || row.background_work_running === true
    : fallbackWorking;
  return liveWorking || listedWorking;
}
