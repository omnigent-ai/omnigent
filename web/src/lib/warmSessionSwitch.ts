import { getCachedServerInfo, isFeatureEnabled } from "@/lib/capabilities";
import { useChatStore } from "@/store/chatStore";

export function warmSwitchKeepMountedEnabled(): boolean {
  return isFeatureEnabled(getCachedServerInfo() ?? "loading", "warm_switch_keep_mounted_ui");
}

// Retain the surface only between a click's source session and its store-first target.
let inFlight: { target: string; fromConversationId: string | null; fromPathname: string } | null =
  null;
let inFlightTimeout: number | undefined;

export function warmSwitchInFlight(
  activeConversationId: string | null,
  urlConvId: string | null | undefined,
): boolean {
  return (
    inFlight !== null &&
    inFlight.target === activeConversationId &&
    inFlight.fromConversationId === (urlConvId ?? null)
  );
}

export function clearWarmSwitchInFlight(): void {
  window.clearTimeout(inFlightTimeout);
  inFlightTimeout = undefined;
  inFlight = null;
}

export function switchSessionBeforeNavigate(conversationId: string): void {
  if (!warmSwitchKeepMountedEnabled()) return;
  const fromConversationId = useChatStore.getState().conversationId;
  if (fromConversationId === conversationId) return;
  clearWarmSwitchInFlight();
  const pending = {
    target: conversationId,
    fromConversationId,
    fromPathname: window.location.pathname,
  };
  inFlight = pending;
  inFlightTimeout = window.setTimeout(() => {
    if (inFlight !== pending) return;
    clearWarmSwitchInFlight();
    if (window.location.pathname === pending.fromPathname) {
      void useChatStore.getState().switchTo(pending.fromConversationId);
    }
  }, 3000);
  void useChatStore.getState().switchTo(conversationId);
}
