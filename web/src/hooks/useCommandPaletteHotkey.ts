// ⌘K (Ctrl+K on Win/Linux) toggles the global command palette. Sibling to the
// session-switch (⌘[ / ⌘]) and sidebar-toggle (⌘⌥[ / ⌘⌥]) hotkeys; like them
// it's bound ONCE at the app shell, where the palette's open-state lives.
//
// Why ⌘K: it's the de-facto command-palette key across developer tools, and
// issue #1059 / PR #1064 deliberately reserved it for this (PR #1064 took ⌘⇧F
// for sidebar search precisely to leave ⌘K free). The browser binds Ctrl+K to
// the address bar, so we preventDefault to claim it.
//
// Platform-aware: only ⌘K fires on macOS and only Ctrl+K on Win/Linux.
// Focused surfaces can still own the chord, but only the variant they actually
// consume — see `focusOwnsHotkey`.

import { useEffect, useRef } from "react";

import { hasCommandModifier, isMacPlatform } from "@/lib/hotkeys";

/** Monaco: ⌘K and Ctrl+K are both chord prefixes, on every platform. */
const CHORD_PREFIX_SURFACE = ".monaco-editor";

/** xterm: forwards the CONTROL variant to the PTY as ^K (kill-to-end-of-line). */
const PTY_SURFACE = ".xterm";

/** True when the event is the command-palette chord: the platform command
 *  modifier + K, no Alt/Shift. */
export function isCommandPaletteHotkey(
  e: globalThis.KeyboardEvent,
  isMac: boolean = isMacPlatform(),
): boolean {
  if (!hasCommandModifier(e, isMac) || e.altKey || e.shiftKey) return false;
  // AltGr reports as Ctrl+Alt on some layouts; the altKey check above already
  // rejects it, but guard explicitly so intl typing never triggers the palette.
  if (e.getModifierState("AltGraph")) return false;
  // Match the letter, not a physical code — ⌘ doesn't remap "k" across layouts.
  return e.key === "k" || e.key === "K";
}

/**
 * Does the focused surface consume THIS chord?
 *
 * Monaco takes both variants. A terminal only takes Ctrl+K: xterm turns it into
 * VT ^K, but drops Cmd chords entirely on macOS (Cmd+A is its lone exception),
 * so yielding ⌘K to a focused terminal leaves it dead — nothing opens and the
 * PTY never sees it. The palette claims that variant instead.
 */
function focusOwnsHotkey(e: globalThis.KeyboardEvent): boolean {
  const el = document.activeElement;
  if (!(el instanceof Element)) return false;
  if (el.closest(CHORD_PREFIX_SURFACE) !== null) return true;
  return e.ctrlKey && !e.metaKey && el.closest(PTY_SURFACE) !== null;
}

/**
 * Bind ⌘/Ctrl+K to toggle the command palette. Bind ONCE.
 *
 * @param onToggle Flip the palette open/closed.
 * @param enabled  Pass `false` to disable the hotkey (e.g. embedded in a real
 *   host page, where ⌘K belongs to the host). Defaults to enabled.
 */
export function useCommandPaletteHotkey(
  onToggle: () => void,
  enabled = true,
  isMac = isMacPlatform(),
): void {
  // Held in a ref so the bound handler always calls the latest closure without
  // re-registering on every render.
  const latest = useRef(onToggle);
  latest.current = onToggle;

  useEffect(() => {
    if (!enabled) return;
    const handler = (e: globalThis.KeyboardEvent): void => {
      // Ignore auto-repeat: holding the chord would flap the palette.
      if (e.repeat) return;
      if (!isCommandPaletteHotkey(e, isMac)) return;
      // Leave the chord to a surface that actually consumes it.
      if (focusOwnsHotkey(e)) return;
      // Claim the chord: preventDefault drops the browser default (Ctrl+K
      // focuses the address bar). stopPropagation mirrors the sibling hotkey
      // hooks; no other listener binds ⌘K, so it's belt-and-suspenders.
      e.preventDefault();
      e.stopPropagation();
      latest.current();
    };
    // Capture phase: the desktop shell renders the embed build over a
    // CSS-hidden host page whose own ⌘K listener would otherwise also fire.
    // Binding at the propagation root lets stopPropagation keep the chord
    // from reaching those listeners; focusOwnsHotkey bails BEFORE stopping
    // propagation, so Monaco/terminal chords still flow to their surfaces.
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [enabled, isMac]);
}
