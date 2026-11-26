// A focused embedded page keeps its keystrokes from the window, so this asks the desktop shell
// for the app's own chords (⌘K, the tab chords) and replays each as a keydown the hotkeys hear.

import { useEffect } from "react";

import { isMacPlatform } from "@/lib/hotkeys";
import {
  type BrowserForwardedChord,
  type BrowserForwardedKeydown,
  browserForwardsChords,
  onBrowserForwardedInput,
  setBrowserForwardedChords,
} from "@/lib/nativeBridge";
import type { Chord } from "@/lib/shortcuts/chord";
import { activeShortcutChords, type ShortcutId } from "@/lib/shortcuts/registry";

/**
 * The app chords a focused browser page hands over in the tabs sidebar. Back and Forward stay
 * with the page: ⌘← and ⌘→ move its caret, and off the Mac their Ctrl+Alt chords type AltGr.
 */
export const TABS_SIDEBAR_FORWARDED_SHORTCUTS: readonly ShortcutId[] = [
  "palette.commands",
  "workspace.newTab",
  "workspace.closeTab",
  "workspace.reopenTab",
];

const TABS_SIDEBAR_WITHOUT_REOPEN = TABS_SIDEBAR_FORWARDED_SHORTCUTS.filter(
  (id) => id !== "workspace.reopenTab",
);

/**
 * The tabs sidebar's list now: forwarding moves focus to the app, so with no closed tab to
 * reopen, Reopen's chord stays with the page instead of taking its focus for nothing.
 */
export function tabsSidebarForwardedShortcuts(canReopen: boolean): readonly ShortcutId[] {
  return canReopen ? TABS_SIDEBAR_FORWARDED_SHORTCUTS : TABS_SIDEBAR_WITHOUT_REOPEN;
}

/** `chord` as the physical keys it presses on this platform; null without a key to press. */
export function physicalChord(chord: Chord, mac: boolean): BrowserForwardedChord | null {
  if (chord.code === undefined && chord.key === undefined) return null;
  return {
    ...(chord.code !== undefined ? { code: chord.code } : { key: chord.key }),
    metaKey: mac && !!chord.mod,
    ctrlKey: !!chord.ctrl || (!mac && !!chord.mod),
    altKey: !!chord.alt,
    shiftKey: !!chord.shift,
  };
}

const CARET_KEYS = new Set(["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"]);

/**
 * Whether a page keeps `chord` for writing text, as the desktop shell does: ⌘← and ⌥→ move a
 * text field's caret on the Mac, and off the Mac Ctrl+Alt is how AltGr types characters.
 */
export function pageKeepsChord(chord: BrowserForwardedChord, mac: boolean): boolean {
  if (!chord.metaKey && !chord.ctrlKey) return true;
  if (!mac) return chord.ctrlKey && chord.altKey;
  const caret = chord.key !== undefined && CARET_KEYS.has(chord.key);
  return caret && !chord.ctrlKey;
}

/** The physical chords of `ids` here that a page hands over, once each. */
export function forwardedChordsFor(
  ids: readonly ShortcutId[],
  mac: boolean,
): BrowserForwardedChord[] {
  const seen = new Set<string>();
  const chords: BrowserForwardedChord[] = [];
  for (const id of ids) {
    for (const chord of activeShortcutChords(id, { mac, native: true })) {
      const physical = physicalChord(chord, mac);
      if (!physical || pageKeepsChord(physical, mac)) continue;
      const signature = JSON.stringify(physical);
      if (seen.has(signature)) continue;
      seen.add(signature);
      chords.push(physical);
    }
  }
  return chords;
}

/**
 * Replay a forwarded chord where the app's hotkey listeners hear it. It came from the page, so
 * the element that last had focus in the app doesn't own it: drop that focus and dispatch on
 * the body, so no surface takes the key as its own.
 */
export function replayForwardedKeydown(input: BrowserForwardedKeydown): void {
  const { type: _type, ...init } = input;
  const stale = document.activeElement;
  if (stale instanceof HTMLElement && stale !== document.body) stale.blur();
  document.body.dispatchEvent(
    new KeyboardEvent("keydown", { ...init, bubbles: true, cancelable: true }),
  );
}

/**
 * Hand `ids`' chords from a focused embedded page to the app, on shells that can. An older
 * shell, and an empty list, leave every key with the page.
 */
export function useBrowserForwardedChords(
  ids: readonly ShortcutId[],
  mac: boolean = isMacPlatform(),
): void {
  const key = ids.join("\n");
  useEffect(() => {
    if (!key || !browserForwardsChords()) return;
    const chords = forwardedChordsFor(key.split("\n") as ShortcutId[], mac);
    const unsubscribe = onBrowserForwardedInput(replayForwardedKeydown);
    void setBrowserForwardedChords(chords);
    return () => {
      unsubscribe();
      void setBrowserForwardedChords([]);
    };
  }, [key, mac]);
}
