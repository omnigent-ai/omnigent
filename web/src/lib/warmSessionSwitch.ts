import { getCachedServerInfo, isFeatureEnabled } from "@/lib/capabilities";
import { useChatStore } from "@/store/chatStore";

export function warmSwitchKeepMountedEnabled(): boolean {
  return isFeatureEnabled(getCachedServerInfo() ?? "loading", "warm_switch_keep_mounted_ui");
}

// Retain the surface only between a click's source URL and its store-first target.
let inFlight: { target: string; fromUrlConvId: string | null } | null = null;

export function warmSwitchInFlight(
  activeConversationId: string | null,
  urlConvId: string | null | undefined,
): boolean {
  return (
    inFlight !== null &&
    inFlight.target === activeConversationId &&
    inFlight.fromUrlConvId === (urlConvId ?? null)
  );
}

export function clearWarmSwitchInFlight(): void {
  inFlight = null;
}

export function switchSessionBeforeNavigate(
  conversationId: string,
  fromUrlConvId: string | null,
): void {
  if (!warmSwitchKeepMountedEnabled()) return;
  if (useChatStore.getState().conversationId === conversationId) return;
  inFlight = conversationId === fromUrlConvId ? null : { target: conversationId, fromUrlConvId };
  void useChatStore.getState().switchTo(conversationId);
}
