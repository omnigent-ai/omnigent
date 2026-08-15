// ⌘⌥A (Ctrl+Alt+A on Win/Linux) archives the session you're currently viewing —
// the keyboard route to the row menu's Archive item, so an open chat can be
// filed away without reaching for its kebab. Sibling to the sidebar-toggle
// (⌘⌥[ / ⌘⌥]) and voice-dictation (⌘⌥V) hotkeys; like them it fires even inside
// a focused text field, so a session can be archived mid-compose.
//
// Why this chord: the ⌘⌥ prefix is already this app's "app action" chord, and
// bare ⌘A is Select All. Bind ONCE, where the active session is known.

import { useEffect, useRef } from "react";

/**
 * @param onArchive Archives the active session, or `null` when there's nothing
 *   archivable (no session open, or the viewer doesn't own it) — the chord then
 *   falls through untouched.
 */
export function useArchiveSessionHotkey(onArchive: (() => void) | null): void {
  // Held in a ref so the bound handler always calls the latest closure without
  // re-registering each render.
  const latest = useRef(onArchive);
  latest.current = onArchive;

  useEffect(() => {
    const handler = (e: globalThis.KeyboardEvent): void => {
      // Require Cmd/Ctrl AND Alt, and reject Shift so ⌘⌥⇧ combos stay free.
      if (!(e.metaKey || e.ctrlKey) || !e.altKey || e.shiftKey) return;
      // AltGr often reports as Ctrl+Alt; ignore it so intl-layout typing doesn't
      // archive a session mid-sentence. Guard the call: not every environment
      // implements getModifierState, and an unguarded call there would throw
      // and break the whole keydown handler.
      if (typeof e.getModifierState === "function" && e.getModifierState("AltGraph")) return;
      // Ignore auto-repeat: holding the chord would re-fire the PATCH.
      if (e.repeat) return;
      // Match the physical key, not the character: ⌥ turns "a" into "å" on
      // macOS, but e.code is stable across layouts and modifiers.
      if (e.code !== "KeyA") return;
      const archive = latest.current;
      if (archive === null) return;
      // Claim the chord: preventDefault drops any default action and
      // stopPropagation keeps it from reaching other keydown listeners.
      e.preventDefault();
      e.stopPropagation();
      archive();
    };

    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);
}
