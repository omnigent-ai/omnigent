import { useEffect } from "react";
import { onUiOpenFile } from "@/lib/uiFileRequests";

/** Use the viewer's guarded navigation, only while this session owns the workspace. */
export function useUiOpenFile(
  conversationId: string | undefined,
  enabled: boolean,
  openFile: (path: string) => void,
): void {
  useEffect(() => {
    if (!conversationId || !enabled) return;
    return onUiOpenFile((event, sourceConversationId) => {
      if (sourceConversationId === conversationId) openFile(event.path);
    });
  }, [conversationId, enabled, openFile]);
}
