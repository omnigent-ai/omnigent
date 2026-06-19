// ⌘[ / ⌘] (Ctrl+[ / Ctrl+] on Win/Linux) toggle the left (Conversations) and
// right (Workspace) sidebars. Siblings to the session-switch (⌘↑/↓) and approve
// (⌘↵) hotkeys; like them they fire even in a focused text field, so a panel
// can be collapsed mid-compose.
//
// NOTE: on macOS ⌘[ / ⌘] are the browser's Back / Forward gestures, so the
// handler calls preventDefault to claim them for the sidebars. Bind ONCE at the
// app shell, where the sidebar open-state lives.

import { useEffect, useRef } from "react";

export interface SidebarToggleHandlers {
  /** Flip the left (Conversations) sidebar. Bound to ⌘/Ctrl + [. */
  onToggleLeft: () => void;
  /** Flip the right (Workspace) sidebar. Bound to ⌘/Ctrl + ]. */
  onToggleRight: () => void;
}

export function useSidebarToggleHotkeys(handlers: SidebarToggleHandlers): void {
  // Held in a ref so the bound handler always calls the latest closures without
  // re-registering each render.
  const latest = useRef(handlers);
  latest.current = handlers;

  useEffect(() => {
    const handler = (e: globalThis.KeyboardEvent): void => {
      // Cmd/Ctrl, not Alt/Shift (mirrors the session-switch / approve guards).
      if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return;
      // Ignore auto-repeat: holding the chord would flap the panel open/closed.
      if (e.repeat) return;
      if (e.key === "[") {
        e.preventDefault(); // also suppresses the macOS browser Back gesture
        latest.current.onToggleLeft();
      } else if (e.key === "]") {
        e.preventDefault(); // also suppresses the macOS browser Forward gesture
        latest.current.onToggleRight();
      }
    };

    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);
}
