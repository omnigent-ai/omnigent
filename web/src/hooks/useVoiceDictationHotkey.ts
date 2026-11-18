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

import { ALT_KEY, MOD_KEY } from "@/components/KeyboardShortcut";
import { isMacPlatform } from "@/lib/hotkeys";
import { matchesShortcut } from "@/lib/shortcuts/registry";

/** Glyphs for the chord `isVoiceDictationHotkey` matches; shown in control tooltips. */
export const VOICE_DICTATION_KEYS = [MOD_KEY, ALT_KEY, "V"] as const;

/** Selector for surfaces that own their keystrokes (terminals, code editor). */
const HOTKEY_OWNING_SURFACES = ".xterm, .monaco-editor";

/** True when the event is the voice-dictation chord: the platform command
 *  modifier + Alt + V, no Shift. */
export function isVoiceDictationHotkey(
  e: globalThis.KeyboardEvent,
  isMac: boolean = isMacPlatform(),
): boolean {
  // The browser-safe ⌘⌥ chord by physical key, since ⌥ rewrites "v" → "√" on macOS.
  return matchesShortcut(e, "dictation.toggle", { mac: isMac });
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
export function useVoiceDictationHotkey(
  onToggle: () => void,
  enabled = true,
  isMac = isMacPlatform(),
): void {
  // Held in a ref so the bound handler always calls the latest closure without
  // re-registering on every render (onToggle changes as listening state flips).
  const latest = useRef(onToggle);
  latest.current = onToggle;

  useEffect(() => {
    if (!enabled) return;
    const handler = (e: globalThis.KeyboardEvent): void => {
      // Ignore auto-repeat: holding the chord would flap dictation on/off.
      if (e.repeat) return;
      if (!isVoiceDictationHotkey(e, isMac)) return;
      if (focusOwnsHotkey()) return;
      e.preventDefault();
      e.stopPropagation();
      latest.current();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled, isMac]);
}
