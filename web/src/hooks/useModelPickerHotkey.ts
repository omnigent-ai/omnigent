// Ctrl+Shift+M (every platform, including macOS) opens the composer's model
// picker: the keyboard equivalent of typing bare "/model", reachable without
// leaving the keyboard.
//
// Ctrl, not the Mac Cmd command-modifier: every Cmd form of M collides on
// macOS (⌘M minimizes the window, ⌥⌘M minimizes all, ⌘⇧M is Chrome's profile
// switcher). Ctrl+Shift+M is free on macOS and was already the Win/Linux chord,
// so one binding works everywhere. Like its sibling hotkeys it bails when focus
// sits in a surface that owns its own keys (xterm terminals, the Monaco editor).

import { useEffect, useRef } from "react";

/** Selector for surfaces that own their keystrokes (terminals, code editor). */
const HOTKEY_OWNING_SURFACES = ".xterm, .monaco-editor";

/** True when the event is the model-picker chord: Ctrl + Shift + M, no Cmd/Alt.
 *  Ctrl on every platform (see file header). */
export function isModelPickerHotkey(e: globalThis.KeyboardEvent): boolean {
  // Require Ctrl+Shift, and reject Cmd (macOS) and Alt so no ⌘/⌥ variant
  // matches: ⌘⇧M is Chrome's profile switcher, ⌥ is the minimize-all family.
  if (!e.ctrlKey || e.metaKey || !e.shiftKey || e.altKey) return false;
  // AltGr reports as Ctrl+Alt on some layouts; the !altKey check above already
  // excludes it, but guard explicitly for parity with the sibling hotkeys.
  if (typeof e.getModifierState === "function" && e.getModifierState("AltGraph")) return false;
  // Match the physical key, stable across layouts and Shift's uppercasing of
  // e.key ("m" vs "M").
  return e.code === "KeyM";
}

/** Does focus sit inside a surface that owns its keystrokes (xterm / Monaco)? */
function focusOwnsHotkey(): boolean {
  const el = document.activeElement;
  return el instanceof Element && el.closest(HOTKEY_OWNING_SURFACES) !== null;
}

/**
 * Bind Ctrl+Shift+M to open the composer's model picker.
 *
 * @param onOpen  Open the picker (pass the same nonce bump the "/model" submit
 *   path uses).
 * @param enabled Mirror the gear's open path (a model picker exists and the
 *   gear is not disabled: not read-only, unreachable, or busy); pass `false` to
 *   leave the chord untouched when there's nothing to open. Defaults on.
 */
export function useModelPickerHotkey(onOpen: () => void, enabled = true): void {
  // Held in a ref so the bound handler always calls the latest closure without
  // re-registering on every render.
  const latest = useRef(onOpen);
  latest.current = onOpen;

  useEffect(() => {
    if (!enabled) return;
    const handler = (e: globalThis.KeyboardEvent): void => {
      // Ignore auto-repeat: holding the chord would reopen the picker.
      if (e.repeat) return;
      if (!isModelPickerHotkey(e)) return;
      if (focusOwnsHotkey()) return;
      e.preventDefault();
      e.stopPropagation();
      latest.current();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled]);
}
