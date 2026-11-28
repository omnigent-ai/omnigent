// ⌘⌥↑ / ⌘⌥↓ (Ctrl+Alt elsewhere) step between the user's messages in a conversation.

import { useEffect, useRef } from "react";

import { matchesShortcut } from "@/lib/shortcuts/registry";

interface MessageNavTarget {
  goPrev: () => void;
  goNext: () => void;
}

/**
 * Bind the message-step chords while the conversation is mounted and `enabled`, which callers
 * tie to `quick_pick_palette`: the registry doesn't check a binding's flag.
 */
export function useMessageNavHotkeys(nav: MessageNavTarget, enabled: boolean): void {
  // Bound once, so a caller's inline target doesn't re-add the listener every render.
  const latestNav = useRef(nav);
  latestNav.current = nav;

  useEffect(() => {
    if (!enabled) return;
    const handler = (event: globalThis.KeyboardEvent) => {
      const previous = matchesShortcut(event, "message.previous");
      if (!previous && !matchesShortcut(event, "message.next")) return;
      // A focused editor that took the chord (adding a cursor, say) keeps it.
      if (event.defaultPrevented) return;
      event.preventDefault();
      if (previous) latestNav.current.goPrev();
      else latestNav.current.goNext();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled]);
}
