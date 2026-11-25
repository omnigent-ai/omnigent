import { useEffect, useRef, useSyncExternalStore } from "react";

import {
  handedOverConversationId,
  handOffToDesktopApp,
  subscribeToDesktopAppHandoff,
} from "@/lib/desktopAppHandoff";

/**
 * Hand the conversation a tab was opened on to the running desktop app.
 *
 * Only the address the tab started with counts: a conversation the user moves
 * to afterwards stays in the browser, where they chose to be.
 *
 * @param conversationId - The route's server session id; undefined off a
 *   conversation, or while a new session has no server id yet.
 * @param plainConversation - False for an address the app would open
 *   differently (a file, or the Canvas view), which stays in the browser.
 * @returns The conversation the app took, while the page should say so
 *   instead of showing it; null otherwise.
 */
export function useDesktopAppHandoff(
  conversationId: string | undefined,
  plainConversation: boolean,
): string | null {
  const landing = useRef(plainConversation ? conversationId : undefined);
  useEffect(() => {
    if (landing.current) void handOffToDesktopApp(landing.current);
  }, []);
  return useSyncExternalStore(subscribeToDesktopAppHandoff, handedOverConversationId);
}
