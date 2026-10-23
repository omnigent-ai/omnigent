// ⌘⌥⇧[ / ⌘⌥⇧] (Ctrl+Alt+Shift on Win/Linux) focus the previous / next pane: a
// grid's cells, or the terminal view's split panes. Grid cells never split, so
// one chord serves both. Brackets echo the ⌘[ / ⌘] session step; Shift keeps it
// clear of the ⌘⌥[ / ⌘⌥] sidebar toggles.

import { useEffect, useRef } from "react";

import { hasCommandModifier, isMacPlatform } from "@/lib/hotkeys";

/** -1 for Cmd/Ctrl+Alt+Shift+[, 1 for Cmd/Ctrl+Alt+Shift+], 0 for anything else. */
export function paneCycleHotkeyStep(
  event: globalThis.KeyboardEvent,
  isMac = isMacPlatform(),
): -1 | 0 | 1 {
  if (!hasCommandModifier(event, isMac) || !event.altKey || !event.shiftKey || event.isComposing) {
    return 0;
  }
  if (typeof event.getModifierState === "function" && event.getModifierState("AltGraph")) {
    return 0;
  }
  // Match the physical key: Alt and Shift change the produced character.
  if (event.code === "BracketRight") return 1;
  if (event.code === "BracketLeft") return -1;
  return 0;
}

interface PaneCycleHotkeyOptions {
  enabled: boolean;
  /** Panes in cycle order; must include ``focusedKey``. */
  paneKeys: readonly string[];
  focusedKey: string | undefined;
  onFocusPane: (key: string) => void;
}

/** Bind the pane-cycle chord, wrapping at either end of ``paneKeys``. */
export function usePaneCycleHotkey(options: PaneCycleHotkeyOptions, isMac = isMacPlatform()): void {
  const latest = useRef(options);
  latest.current = options;

  useEffect(() => {
    if (!options.enabled) return;

    const handler = (event: globalThis.KeyboardEvent): void => {
      const step = paneCycleHotkeyStep(event, isMac);
      if (event.repeat || step === 0) return;
      event.preventDefault();
      event.stopPropagation();
      const { paneKeys, focusedKey, onFocusPane } = latest.current;
      const index = paneKeys.indexOf(focusedKey ?? "");
      onFocusPane(paneKeys[(index + step + paneKeys.length) % paneKeys.length]);
    };

    // Capture before a focused xterm can forward the chord to its PTY.
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [isMac, options.enabled]);
}
