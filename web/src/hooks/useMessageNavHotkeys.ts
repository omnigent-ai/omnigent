// ⌘⌥↑ / ⌘⌥↓ (Ctrl+Alt elsewhere) step between the user's messages in a conversation.

import { useEffect } from "react";

import { matchesShortcut } from "@/lib/shortcuts/registry";

interface MessageNavTarget {
  goPrev: () => void;
  goNext: () => void;
}

/** Bind the message-step chords while the conversation is mounted. */
export function useMessageNavHotkeys(nav: MessageNavTarget): void {
  useEffect(() => {
    const handler = (event: globalThis.KeyboardEvent) => {
      const previous = matchesShortcut(event, "message.previous");
      if (!previous && !matchesShortcut(event, "message.next")) return;
      // A focused editor that took the chord (adding a cursor, say) keeps it.
      if (event.defaultPrevented) return;
      event.preventDefault();
      if (previous) nav.goPrev();
      else nav.goNext();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [nav]);
}
