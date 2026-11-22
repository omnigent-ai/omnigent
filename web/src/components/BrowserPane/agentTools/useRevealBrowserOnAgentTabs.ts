// Show the Browser pane when an agent opens or selects a tab
// in the session on screen (browser_tabs new / select). The relay records the
// tab change itself; this only reveals the pane.
import { useEffect, useRef } from "react";
import { onBrowserActionRequest } from "@/lib/browserActionBus";
import { supportsBrowser } from "@/lib/nativeBridge";

/**
 * What the agent asked for: a new tab, whose id the relay picks, or one it already has.
 * `actionId` names the request, as the relay's outcome for it does.
 */
export type AgentBrowserTabRequest = { actionId: string } & (
  { action: "new" } | { action: "select"; tabId: string | null }
);

export function useRevealBrowserOnAgentTabs(
  conversationId: string | null | undefined,
  reveal: (request: AgentBrowserTabRequest) => void,
): void {
  const revealRef = useRef(reveal);
  useEffect(() => {
    revealRef.current = reveal;
  });
  useEffect(() => {
    // Only a shell that shows the embedded browser: every viewer of the session gets
    // the request, and a web viewer has no Browser pane to reveal.
    if (!supportsBrowser()) return undefined;
    return onBrowserActionRequest((evt, sourceConversationId) => {
      if (evt.action !== "tabs" || !sourceConversationId) return;
      if (sourceConversationId !== conversationId) return;
      if (evt.args.action === "new") revealRef.current({ actionId: evt.actionId, action: "new" });
      if (evt.args.action === "select") {
        const tabId = typeof evt.args.tab_id === "string" ? evt.args.tab_id : null;
        revealRef.current({ actionId: evt.actionId, action: "select", tabId });
      }
    });
  }, [conversationId]);
}
