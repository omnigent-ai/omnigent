// ⌘⌥V (Ctrl+Alt+V on Win/Linux) toggles voice dictation — the same action as
// clicking the composer mic button, reachable from anywhere in the app so you
// can start/stop talking without leaving the keyboard (WhisperFlow-style).
//
// Why this chord: single-modifier letter combos collide with OS/browser
// defaults (⌘M minimizes the window on macOS; most ⌘⇧-letter combos are browser
// shortcuts). Adding ⌥ dodges both — it shares the browser-safe ⌘⌥ chord used by
// the sidebar-toggle (⌘⌥[ / ⌘⌥]) and pinned-session (⌘⌥1–0) hotkeys. Like its
// siblings it bails when focus sits in a surface that owns its own keys (xterm
// terminals, the Monaco editor).

import { useEffect, useRef } from "react";

/** Selector for surfaces that own their keystrokes (terminals, code editor). */
const HOTKEY_OWNING_SURFACES = ".xterm, .monaco-editor";

/** True when the event is the voice-dictation chord: Cmd/Ctrl+Alt+V, no Shift. */
export function isVoiceDictationHotkey(e: globalThis.KeyboardEvent): boolean {
  // Require Cmd/Ctrl AND Alt (the browser-safe ⌘⌥ chord) and reject Shift, so
  // ⌘⌥⇧ combos stay free for future bindings.
  if (!(e.metaKey || e.ctrlKey) || !e.altKey || e.shiftKey) return false;
  // AltGr often reports as Ctrl+Alt; ignore it so intl-layout typing doesn't
  // trigger dictation. Guard the call: not every environment implements
  // getModifierState, and an unguarded call there would throw.
  if (typeof e.getModifierState === "function" && e.getModifierState("AltGraph")) return false;
  // Match the physical key, not the character: ⌥ rewrites "v" → "√" on macOS,
  // but e.code is stable across layouts and modifiers.
  return e.code === "KeyV";
}

/** Does focus sit inside a surface that owns its keystrokes (xterm / Monaco)? */
function focusOwnsHotkey(): boolean {
  const el = document.activeElement;
  return el instanceof Element && el.closest(HOTKEY_OWNING_SURFACES) !== null;
}

/**
 * Bind ⌘/Ctrl+⌥/Alt+V to toggle voice dictation.
 *
 * @param onToggle Start or stop the recognizer (the mic button's toggle).
 * @param enabled  Pass `false` to skip binding (e.g. the secondary composer in
 *   the New Chat dialog, so two mics don't fight for the device). Defaults on.
 */
export function useVoiceDictationHotkey(onToggle: () => void, enabled: boolean = true): void {
  // Held in a ref so the bound handler always calls the latest closure without
  // re-registering on every render (onToggle changes as listening state flips).
  const latest = useRef(onToggle);
  latest.current = onToggle;

  useEffect(() => {
    if (!enabled) return;
    const handler = (e: globalThis.KeyboardEvent): void => {
      // Ignore auto-repeat: holding the chord would flap dictation on/off.
      if (e.repeat) return;
      if (!isVoiceDictationHotkey(e)) return;
      if (focusOwnsHotkey()) return;
      e.preventDefault();
      e.stopPropagation();
      latest.current();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled]);
}
