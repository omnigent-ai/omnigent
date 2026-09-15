import { useEffect } from "react";
import { useChatStore } from "@/store/chatStore";

let visibleOwner: symbol | null = null;

/** Bind one visible chat; closing it deactivates the projection, not the background agent. */
export function useChatSessionBinding(conversationId: string | undefined) {
  useEffect(() => {
    const owner = Symbol("visible-chat");
    visibleOwner = owner;
    const id = conversationId ?? null;
    void useChatStore.getState().switchTo(id);
    return () => {
      // A replacement view or StrictMode replay claims ownership before this runs.
      queueMicrotask(() => {
        if (visibleOwner !== owner) return;
        visibleOwner = null;
        const state = useChatStore.getState();
        if (state.conversationId === id) void state.switchTo(null);
      });
    };
  }, [conversationId]);
}
