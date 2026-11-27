import { matchPath } from "react-router-dom";
import { withBasePath } from "@/lib/basePath";
import { useRebasePath, type RoutingApi } from "@/lib/routing";
import { getCachedServerInfo, isFeatureEnabled } from "@/lib/capabilities";
import { isTempConvId } from "@/lib/tempConversationId";
import { isConversationStreamCurrent, useChatStore } from "@/store/chatStore";

export function warmSwitchKeepMountedEnabled(): boolean {
  return isFeatureEnabled(getCachedServerInfo() ?? "loading", "warm_switch_keep_mounted_ui");
}

function conversationIdFromPath(
  pathname: string,
  rebasePath: RoutingApi["rebasePath"],
): string | undefined {
  // Routing owns embed mounts; the server base path covers standalone proxies.
  return matchPath(withBasePath(rebasePath("/c/:conversationId")), pathname)?.params.conversationId;
}

export function isWarmSwitchPending(
  activeConversationId: string | null,
  urlConvId: string | null | undefined,
  rebasePath: RoutingApi["rebasePath"],
): boolean {
  // Read the browser path during render: history moves before React commits the route.
  return (
    warmSwitchKeepMountedEnabled() &&
    activeConversationId !== null &&
    activeConversationId !== urlConvId &&
    conversationIdFromPath(window.location.pathname, rebasePath) === activeConversationId
  );
}

export function useWarmSwitchConversationId(urlConvId: string | undefined): string | undefined {
  const activeConversationId = useChatStore((state) => state.conversationId);
  const rebasePath = useRebasePath();
  return isWarmSwitchPending(activeConversationId, urlConvId, rebasePath)
    ? (activeConversationId ?? undefined)
    : urlConvId;
}

export function switchSessionBeforeNavigate(
  conversationId: string,
  rebasePath: RoutingApi["rebasePath"],
): void {
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
  const sourceConversationId =
    conversationIdFromPath(fromPathname, rebasePath) ?? fromConversationId;
  if (isTempConvId(sourceConversationId)) return;
  window.setTimeout(() => {
    if (
      window.location.pathname === fromPathname &&
      useChatStore.getState().conversationId === conversationId
    ) {
      void useChatStore.getState().switchTo(sourceConversationId);
    }
  }, 0);
  void useChatStore.getState().switchTo(conversationId);
}
