// Show the Browser pane when an agent opens or selects a tab
// in the session on screen (browser_tabs new / select). The relay records the
// tab change itself; this only reveals the pane.
import { useEffect, useRef } from "react";
import { onBrowserActionRequest } from "@/lib/browserActionBus";

export function useRevealBrowserOnAgentTabs(
  conversationId: string | null | undefined,
  reveal: () => void,
): void {
  const revealRef = useRef(reveal);
  useEffect(() => {
    revealRef.current = reveal;
  });
  useEffect(
    () =>
      onBrowserActionRequest((evt, sourceConversationId) => {
        if (
          evt.action === "tabs" &&
          (evt.args.action === "new" || evt.args.action === "select") &&
          sourceConversationId &&
          sourceConversationId === conversationId
        ) {
          revealRef.current();
        }
      }),
    [conversationId],
  );
}
