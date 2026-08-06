// The two chords that open the keyboard overlay, kept together because they
// share every guard and differ only in which mode they request:
//
//   ⌘K  / Ctrl+K        → command palette (app commands)
//   ⌘⇧F / Ctrl+Shift+F  → session search
//
// Splitting them follows VS Code, where the command palette and the search view
// sit on separate keys because they are separate tasks. Both are bound ONCE at
// the app shell, where the overlay's open-state lives — siblings to the
// session-switch (⌘↑/↓) and sidebar-toggle (⌘⌥[ / ⌘⌥]) hotkeys.
//
// ⌘K is the de-facto command-palette key across developer tools; ⌘⇧F is the
// de-facto content-search key, and Omnigent's session search matches chat
// content, not just titles. The browser binds Ctrl+K to the address bar, so we
// preventDefault to claim the chords.
//
// Two surfaces own these chords themselves and must keep them: xterm terminals
// (forward them to the PTY) and the Monaco editor (⌘K is a chord prefix there,
// ⌘⇧F its own find). When focus sits in one of those, we bail and let the
// keystroke through.

import { useEffect, useRef } from "react";

/** Selector for surfaces that own these chords (terminals, code editor). */
const HOTKEY_OWNING_SURFACES = ".xterm, .monaco-editor";

/** True when the modifier state is Cmd/Ctrl plus exactly `shift`, and no Alt. */
function hasModifiers(e: globalThis.KeyboardEvent, shift: boolean): boolean {
  if (!(e.metaKey || e.ctrlKey) || e.altKey) return false;
  if (e.shiftKey !== shift) return false;
  // AltGr reports as Ctrl+Alt on some layouts; the altKey check above already
  // rejects it, but guard explicitly so intl typing never opens the overlay.
  return !e.getModifierState("AltGraph");
}

/** True when the event is the command-palette chord: Cmd/Ctrl+K, no Alt/Shift. */
export function isCommandPaletteHotkey(e: globalThis.KeyboardEvent): boolean {
  // Match the letter, not a physical code — ⌘ doesn't remap "k" across layouts.
  return hasModifiers(e, false) && (e.key === "k" || e.key === "K");
}

/** True when the event is the session-search chord: Cmd/Ctrl+Shift+F. */
export function isSessionSearchHotkey(e: globalThis.KeyboardEvent): boolean {
  return hasModifiers(e, true) && (e.key === "f" || e.key === "F");
}

/** Does focus sit inside a surface that owns these chords (xterm / Monaco)? */
function focusOwnsHotkey(): boolean {
  const el = document.activeElement;
  return el instanceof Element && el.closest(HOTKEY_OWNING_SURFACES) !== null;
}

/** Shared body of both hooks: bind `match` on window, fire `onToggle`. */
function usePaletteHotkey(
  match: (e: globalThis.KeyboardEvent) => boolean,
  onToggle: () => void,
  enabled: boolean,
): void {
  // Held in a ref so the bound handler always calls the latest closure without
  // re-registering on every render.
  const latest = useRef(onToggle);
  latest.current = onToggle;

  useEffect(() => {
    if (!enabled) return;
    const handler = (e: globalThis.KeyboardEvent): void => {
      // Ignore auto-repeat: holding the chord would flap the overlay.
      if (e.repeat) return;
      if (!match(e)) return;
      // Leave the chord to terminals/editors that bind it themselves.
      if (focusOwnsHotkey()) return;
      // Claim it: preventDefault drops the browser default (Ctrl+K focuses the
      // address bar). stopPropagation mirrors the sibling hotkey hooks.
      e.preventDefault();
      e.stopPropagation();
      latest.current();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [match, enabled]);
}

/**
 * Bind ⌘/Ctrl+K to toggle the command palette. Bind ONCE.
 *
 * @param onToggle Flip the command palette open/closed.
 * @param enabled  Pass `false` to disable the hotkey (e.g. embedded mode, where
 *   ⌘K belongs to the host page). Defaults to enabled.
 */
export function useCommandPaletteHotkey(onToggle: () => void, enabled = true): void {
  usePaletteHotkey(isCommandPaletteHotkey, onToggle, enabled);
}

/**
 * Bind ⌘/Ctrl+Shift+F to toggle session search. Bind ONCE.
 *
 * @param onToggle Flip session search open/closed.
 * @param enabled  Pass `false` to disable the hotkey (e.g. embedded mode).
 *   Defaults to enabled.
 */
export function useSessionSearchHotkey(onToggle: () => void, enabled = true): void {
  usePaletteHotkey(isSessionSearchHotkey, onToggle, enabled);
}
