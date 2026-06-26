// ⌘K (Ctrl+K on Win/Linux) toggles the global command palette. Sibling to the
// session-switch (⌘↑/↓) and sidebar-toggle (⌘⌥[ / ⌘⌥]) hotkeys; like them it's
// bound ONCE at the app shell, where the palette's open-state lives.
//
// Why ⌘K: it's the de-facto command-palette key across developer tools, and
// issue #1059 / PR #1064 deliberately reserved it for this (PR #1064 took ⌘⇧F
// for sidebar search precisely to leave ⌘K free). The browser binds Ctrl+K to
// the address bar, so we preventDefault to claim it.
//
// Two surfaces own ⌘K themselves and must keep it: xterm terminals (forward it
// to the PTY) and the Monaco editor (⌘K is a chord prefix). When focus sits in
// one of those, we bail and let the keystroke through.

import { useEffect, useRef } from "react";

/** Selector for surfaces that own ⌘K and must keep it (terminals, code editor). */
const HOTKEY_OWNING_SURFACES = ".xterm, .monaco-editor";

/** True when the event is the command-palette chord: Cmd/Ctrl+K, no Alt/Shift. */
export function isCommandPaletteHotkey(e: globalThis.KeyboardEvent): boolean {
  if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return false;
  // AltGr reports as Ctrl+Alt on some layouts; the altKey check above already
  // rejects it, but guard explicitly so intl typing never triggers the palette.
  if (e.getModifierState("AltGraph")) return false;
  // Match the letter, not a physical code — ⌘ doesn't remap "k" across layouts.
  return e.key === "k" || e.key === "K";
}

/** Does focus sit inside a surface that owns ⌘K (xterm / Monaco)? */
function focusOwnsHotkey(): boolean {
  const el = document.activeElement;
  return el instanceof Element && el.closest(HOTKEY_OWNING_SURFACES) !== null;
}

/**
 * Bind ⌘/Ctrl+K to toggle the command palette. Bind ONCE.
 *
 * @param onToggle Flip the palette open/closed.
 * @param enabled  Pass `false` to disable the hotkey (e.g. embedded mode, where
 *   ⌘K belongs to the host page). Defaults to enabled.
 */
export function useCommandPaletteHotkey(onToggle: () => void, enabled: boolean = true): void {
  // Held in a ref so the bound handler always calls the latest closure without
  // re-registering on every render.
  const latest = useRef(onToggle);
  latest.current = onToggle;

  useEffect(() => {
    if (!enabled) return;
    const handler = (e: globalThis.KeyboardEvent): void => {
      // Ignore auto-repeat: holding the chord would flap the palette.
      if (e.repeat) return;
      if (!isCommandPaletteHotkey(e)) return;
      // Leave ⌘K to terminals/editors that bind it themselves.
      if (focusOwnsHotkey()) return;
      // Claim the chord: preventDefault drops the browser default (Ctrl+K
      // focuses the address bar). stopPropagation mirrors the sibling hotkey
      // hooks; no other listener binds ⌘K, so it's belt-and-suspenders.
      e.preventDefault();
      e.stopPropagation();
      latest.current();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled]);
}
