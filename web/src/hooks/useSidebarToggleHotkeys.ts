// ⌘⌥[ / ⌘⌥] (Ctrl+Alt+[ / Ctrl+Alt+] on Win/Linux) toggle the left
// (Conversations) and right (Workspace) sidebars. Siblings to the session-switch
// (⌘↑/↓) and approve (⌘↵) hotkeys; like them they fire even inside a focused
// text field, so a panel can be collapsed mid-compose.
//
// Why this chord: the bare ⌘[ / ⌘] are the browser's Back/Forward gestures, and
// single ⌘+punctuation combos (e.g. ⌘\) get swallowed by global hotkey utilities
// (Raycast/Rectangle/…) before the page ever sees them. Adding ⌥ dodges both —
// it's not a browser gesture and is essentially never grabbed system-wide — and
// it shares the ⌘⌥ chord with ChatPage's message-nav hotkey. Bind ONCE at the
// app shell, where the sidebar open-state lives.

import { useEffect, useRef } from "react";

export interface SidebarToggleHandlers {
  /** Flip the left (Conversations) sidebar. Bound to ⌘/Ctrl + ⌥/Alt + [. */
  onToggleLeft: () => void;
  /** Flip the right (Workspace) sidebar. Bound to ⌘/Ctrl + ⌥/Alt + ]. */
  onToggleRight: () => void;
}

export function useSidebarToggleHotkeys(handlers: SidebarToggleHandlers): void {
  // Held in a ref so the bound handler always calls the latest closures without
  // re-registering each render.
  const latest = useRef(handlers);
  latest.current = handlers;

  useEffect(() => {
    const handler = (e: globalThis.KeyboardEvent): void => {
      // Require Cmd/Ctrl AND Alt (the ⌘⌥ message-nav chord) and additionally
      // reject Shift, so ⌘⌥⇧ combos stay free for future bindings.
      if (!(e.metaKey || e.ctrlKey) || !e.altKey || e.shiftKey) return;
      // AltGr often reports as Ctrl+Alt; ignore it so intl-layout typing doesn't
      // accidentally toggle sidebars while focused in an editor/composer. Guard
      // the call: not every environment implements getModifierState, and an
      // unguarded call there would throw and break the whole keydown handler.
      if (typeof e.getModifierState === "function" && e.getModifierState("AltGraph")) return;
      // Ignore auto-repeat: holding the chord would flap the panel open/closed.
      if (e.repeat) return;
      // Match the physical key, not the character: ⌥ turns "[" / "]" into "“" /
      // "‘" on macOS, but e.code is stable across layouts and modifiers.
      // Claim the chord: preventDefault drops any default action and
      // stopPropagation keeps it from reaching other keydown listeners.
      if (e.code === "BracketLeft") {
        e.preventDefault();
        e.stopPropagation();
        latest.current.onToggleLeft();
      } else if (e.code === "BracketRight") {
        e.preventDefault();
        e.stopPropagation();
        latest.current.onToggleRight();
      }
    };

    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);
}
