// Ctrl+Shift+L (every platform, including macOS) focuses the composer's chat
// input, so the user can start typing from anywhere in the session view without
// reaching for the mouse.
//
// Ctrl, not the Mac Cmd command-modifier, and Ctrl+Shift+<letter> for parity
// with its sibling Ctrl+Shift+M model-picker chord — that family is free on
// every platform. L is unbound in Chrome/Firefox/Electron (unlike Ctrl+Shift+
// I/J/C, which open devtools). Like its siblings it bails when focus sits in a
// surface that owns its own keys (xterm terminals, the Monaco editor).

import { useEffect, useRef } from "react";

/** Selector for surfaces that own their keystrokes (terminals, code editor). */
const HOTKEY_OWNING_SURFACES = ".xterm, .monaco-editor";

/** True when the event is the focus-composer chord: Ctrl + Shift + L, no Cmd/Alt.
 *  Ctrl on every platform (see file header). */
export function isFocusComposerHotkey(e: globalThis.KeyboardEvent): boolean {
  // Require Ctrl+Shift, and reject Cmd (macOS) and Alt so no ⌘/⌥ variant matches.
  if (!e.ctrlKey || e.metaKey || !e.shiftKey || e.altKey) return false;
  // AltGr reports as Ctrl+Alt on some layouts; the !altKey check above already
  // excludes it, but guard explicitly for parity with the sibling hotkeys.
  if (typeof e.getModifierState === "function" && e.getModifierState("AltGraph")) return false;
  // Match the physical key, stable across layouts and Shift's uppercasing.
  return e.code === "KeyL";
}

/** Does focus sit inside a surface that owns its keystrokes (xterm / Monaco)? */
function focusOwnsHotkey(): boolean {
  const el = document.activeElement;
  return el instanceof Element && el.closest(HOTKEY_OWNING_SURFACES) !== null;
}

/**
 * Bind Ctrl+Shift+L to focus the composer's chat input.
 *
 * @param onFocus Focus the composer textarea (callers no-op on mobile, where
 *   programmatic focus would summon the software keyboard).
 * @param enabled Pass `false` to leave the chord untouched (e.g. no composer on
 *   screen). Defaults on.
 */
export function useFocusComposerHotkey(onFocus: () => void, enabled = true): void {
  // Held in a ref so the bound handler always calls the latest closure without
  // re-registering on every render.
  const latest = useRef(onFocus);
  latest.current = onFocus;

  useEffect(() => {
    if (!enabled) return;
    const handler = (e: globalThis.KeyboardEvent): void => {
      // Ignore auto-repeat: holding the chord would re-fire focus pointlessly.
      if (e.repeat) return;
      if (!isFocusComposerHotkey(e)) return;
      if (focusOwnsHotkey()) return;
      e.preventDefault();
      e.stopPropagation();
      latest.current();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled]);
}
