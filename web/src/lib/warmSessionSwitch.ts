import { matchPath } from "react-router-dom";
import { getBasePath } from "@/lib/basePath";
import { getCachedServerInfo, isFeatureEnabled } from "@/lib/capabilities";
import { isTempConvId } from "@/lib/tempConversationId";
import { isConversationStreamCurrent, useChatStore } from "@/store/chatStore";

export function warmSwitchKeepMountedEnabled(): boolean {
  return isFeatureEnabled(getCachedServerInfo() ?? "loading", "warm_switch_keep_mounted_ui");
}

export function isWarmSwitchPending(
  activeConversationId: string | null,
  urlConvId: string | null | undefined,
): boolean {
  // Read the browser path during render: history moves before React's transitioned location.
  return (
    warmSwitchKeepMountedEnabled() &&
    activeConversationId !== null &&
    activeConversationId !== urlConvId &&
    matchPath(`${getBasePath()}/c/:conversationId`, window.location.pathname)?.params
      .conversationId === activeConversationId
  );
}

export function switchSessionBeforeNavigate(conversationId: string): void {
  if (!warmSwitchKeepMountedEnabled()) return;
  const fromConversationId = useChatStore.getState().conversationId;
  if (
    fromConversationId === null ||
    fromConversationId === conversationId ||
    isTempConvId(fromConversationId) ||
    isTempConvId(conversationId) ||
    !isConversationStreamCurrent(conversationId)
  )
    return;
  const fromPathname = window.location.pathname;
  window.setTimeout(() => {
    if (window.location.pathname === fromPathname) {
      void useChatStore.getState().switchTo(fromConversationId);
    }
  }, 0);
  void useChatStore.getState().switchTo(conversationId);
}
