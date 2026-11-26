import { getCachedServerInfo, isFeatureEnabled } from "@/lib/capabilities";
import { useChatStore } from "@/store/chatStore";

/** Whether in-app session switches keep the chat surface mounted. */
export function warmSwitchKeepMountedEnabled(): boolean {
  return isFeatureEnabled(getCachedServerInfo() ?? "loading", "warm_switch_keep_mounted_ui");
}

/**
 * The session a click-initiated switch has already moved the store to, while
 * its navigation commit is still in flight. Only `switchSessionBeforeNavigate`
 * sets this, so Back/Forward, deep links and reloads never see it.
 */
let inFlightTarget: string | null = null;

/**
 * Whether the store is in a click's switch-ahead window for
 * *activeConversationId*: the surface can keep rendering the coherent target
 * state for the frame before the URL catches up, instead of unmounting behind
 * the hydrating placeholder.
 */
export function warmSwitchInFlight(activeConversationId: string | null): boolean {
  return inFlightTarget !== null && inFlightTarget === activeConversationId;
}

/** Close the switch-ahead window once the URL has landed on the target. */
export function clearWarmSwitchInFlight(): void {
  inFlightTarget = null;
}

/**
 * Switches the chat store before a click's navigation lands, so the surface
 * never tears down behind the hydrating placeholder. The store write re-renders
 * a frame before the router's transition commit (external-store updates are
 * synchronous), which is why ChatPage's gate reads `warmSwitchInFlight` for
 * that window. The route effect's `switchTo` stays the fallback for every
 * other path and self-skips when this already ran.
 *
 * Call from a click handler that then navigates.
 */
export function switchSessionBeforeNavigate(conversationId: string): void {
  if (!warmSwitchKeepMountedEnabled()) return;
  inFlightTarget = conversationId;
  void useChatStore.getState().switchTo(conversationId);
}
