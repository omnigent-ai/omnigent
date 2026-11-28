// Toggle the left (Conversations) and right (Workspace) sidebars: ⌘B / ⇧⌘B
// (Ctrl+B / Ctrl+Shift+B on Win/Linux) in the desktop app, ⌘⌥[ / ⌘⌥] (Ctrl+Alt)
// in a browser tab, where ⌘B and ⇧⌘B open the browser's bookmarks. Like the
// approve (⌘↵) hotkey they fire even inside a focused text field, so a panel can
// be collapsed mid-compose. Platform-aware: only the ⌘ chord fires on macOS and
// only the Ctrl chord on Win/Linux.
//
// Why ⌘⌥ in the browser: the bare ⌘[ / ⌘] are the browser's Back/Forward
// gestures, and single ⌘+punctuation combos (e.g. ⌘\) get swallowed by global
// hotkey utilities (Raycast/Rectangle/…) before the page ever sees them. Bind
// ONCE at the app shell, where the sidebar open-state lives (a grid binds its
// own right-side toggle).

import { useEffect, useRef } from "react";

import { isMacPlatform } from "@/lib/hotkeys";
import { isNativeShell } from "@/lib/nativeBridge";
import { matchesShortcut } from "@/lib/shortcuts/registry";

export interface SidebarToggleHandlers {
  /** Flip the left (Conversations) sidebar. */
  onToggleLeft?: () => void;
  /** Flip the right (Workspace) sidebar. Unset sides stay unclaimed. */
  onToggleRight?: () => void;
}

// Rich-text editors keep ⌘B for Bold.
const RICH_TEXT_SELECTOR = "[contenteditable]:not([contenteditable='false'])";

/** The sidebar a keydown toggles, or null when it isn't a sidebar chord here. */
export function sidebarToggleSide(
  e: globalThis.KeyboardEvent,
  isMac = isMacPlatform(),
  native = isNativeShell(),
): "left" | "right" | null {
  // AltGr often reports as Ctrl+Alt; the chords ignore it so intl-layout typing
  // doesn't toggle sidebars while focused in an editor or composer.
  const environment = { mac: isMac, native };
  const side = matchesShortcut(e, "sidebar.left", environment)
    ? "left"
    : matchesShortcut(e, "sidebar.right", environment)
      ? "right"
      : null;
  if (side === null) return null;
  const focused = document.activeElement;
  if (native) {
    if (side === "right") return side;
    if (focused?.closest(RICH_TEXT_SELECTOR)) return null;
    // Ctrl+B is the tmux prefix in a terminal off macOS.
    if (!isMac && focused?.closest(".xterm")) return null;
    return side;
  }
  // Monaco folds with ⌘⌥[ / ⌘⌥] on macOS (Ctrl+Shift elsewhere).
  if (isMac && focused?.closest(".monaco-editor")) return null;
  return side;
}

export function useSidebarToggleHotkeys(
  handlers: SidebarToggleHandlers,
  isMac = isMacPlatform(),
): void {
  // Held in a ref so the bound handler always calls the latest closures without
  // re-registering each render.
  const latest = useRef(handlers);
  latest.current = handlers;

  useEffect(() => {
    const handler = (e: globalThis.KeyboardEvent): void => {
      const side = sidebarToggleSide(e, isMac);
      const toggle =
        side === "left"
          ? latest.current.onToggleLeft
          : side === "right"
            ? latest.current.onToggleRight
            : undefined;
      if (!toggle) return;
      // Claim the chord: preventDefault drops any default action and
      // stopPropagation keeps it from reaching other keydown listeners.
      e.preventDefault();
      e.stopPropagation();
      // Claim auto-repeat too, but act once: holding the chord would flap the panel.
      if (e.repeat) return;
      toggle();
    };

    // Capture before a focused xterm encodes Ctrl+Alt+[ / ] for its PTY.
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [isMac]);
}
