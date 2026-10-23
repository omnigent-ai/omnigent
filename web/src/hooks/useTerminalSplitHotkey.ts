// ⌘⌥⇧\ (⌘⌥|) splits the focused terminal pane right and ⌘⌥- splits it down
// (Ctrl+Alt on Win/Linux), skipping the header split menus. The glyphs mirror the
// new divider: | side by side, - stacked. Plain ⌘⌥\ stays the Chat/Terminal toggle.

import { useEffect, useRef } from "react";

import { hasCommandModifier, isMacPlatform } from "@/lib/hotkeys";
import type { TerminalSplitAxis } from "@/lib/terminalSplitLayout";

/** The split a keydown asks for, or null when it isn't a split chord. */
export function terminalSplitHotkeyAxis(
  event: globalThis.KeyboardEvent,
  isMac = isMacPlatform(),
): TerminalSplitAxis | null {
  if (!hasCommandModifier(event, isMac) || !event.altKey || event.isComposing) return null;
  if (typeof event.getModifierState === "function" && event.getModifierState("AltGraph")) {
    return null;
  }
  // Match the physical key: Alt and Shift change the produced character.
  if (event.code === "Backslash" && event.shiftKey) return "columns";
  if (event.code === "Minus" && !event.shiftKey) return "rows";
  return null;
}

/** Bind the chord for ``axis`` while ``enabled``. */
export function useTerminalSplitHotkey(
  axis: TerminalSplitAxis,
  onSplit: () => void,
  enabled: boolean,
  isMac = isMacPlatform(),
): void {
  const latest = useRef(onSplit);
  latest.current = onSplit;

  useEffect(() => {
    if (!enabled) return;

    const handler = (event: globalThis.KeyboardEvent): void => {
      // Ignore auto-repeat: holding the chord would open a shell per tick.
      if (event.repeat || terminalSplitHotkeyAxis(event, isMac) !== axis) return;
      event.preventDefault();
      event.stopPropagation();
      latest.current();
    };

    // Capture before the focused xterm can forward the chord to its PTY.
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [axis, enabled, isMac]);
}
