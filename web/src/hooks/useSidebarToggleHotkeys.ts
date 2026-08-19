// ⌘⌥[ toggles Conversations; ⌘B and the legacy ⌘⌥] toggle Workspace
// (Ctrl equivalents on Win/Linux). Like the other global hotkeys, these fire
// inside focused editable surfaces so a panel can be collapsed mid-edit.
//
// The bracket chords retain Alt to avoid browser Back/Forward gestures. The
// mnemonic Workspace alias deliberately claims the browser's bookmarks chord.
// Bind once at the app shell, where the sidebar open-state lives.

import { useEffect, useRef } from "react";

export interface SidebarToggleHandlers {
  /** Flip the left (Conversations) sidebar. Bound to ⌘/Ctrl + ⌥/Alt + [. */
  onToggleLeft: () => void;
  /** Flip the right (Workspace) sidebar. Bound to ⌘/Ctrl+B and legacy ⌘/Ctrl+⌥/Alt+]. */
  onToggleRight: () => void;
}

export function useSidebarToggleHotkeys(handlers: SidebarToggleHandlers): void {
  // Held in a ref so the bound handler always calls the latest closures without
  // re-registering each render.
  const latest = useRef(handlers);
  latest.current = handlers;

  useEffect(() => {
    const handler = (e: globalThis.KeyboardEvent): void => {
      if (!(e.metaKey || e.ctrlKey) || e.shiftKey || e.repeat) return;

      // Claim the mnemonic before editors or terminals can apply bold/send ^B.
      if (!e.altKey && e.code === "KeyB") {
        e.preventDefault();
        e.stopPropagation();
        latest.current.onToggleRight();
        return;
      }

      // Legacy bracket bindings use the browser-safe Cmd/Ctrl+Alt chord.
      if (!e.altKey) return;
      // AltGr often reports as Ctrl+Alt; ignore it so intl-layout typing doesn't
      // accidentally toggle sidebars while focused in an editor/composer. Guard
      // the call: not every environment implements getModifierState, and an
      // unguarded call there would throw and break the whole keydown handler.
      if (typeof e.getModifierState === "function" && e.getModifierState("AltGraph")) return;
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

    window.addEventListener("keydown", handler, { capture: true });
    return () => window.removeEventListener("keydown", handler, { capture: true });
  }, []);
}
