// ⌘⌥⇧\ (⌘⌥|) splits the focused terminal pane right and ⌘⌥- splits it down
// (Ctrl+Alt on Win/Linux), skipping the header split menus. The glyphs mirror the
// new divider: | side by side, - stacked. Plain ⌘⌥\ stays the Chat/Terminal toggle.

import { useEffect, useRef } from "react";

import { isFocusInDialogOrMenu, isMacPlatform } from "@/lib/hotkeys";
import { matchesShortcut } from "@/lib/shortcuts/registry";
import type { TerminalSplitAxis } from "@/lib/terminalSplitLayout";

/** The split a keydown asks for, or null when it isn't a split chord. */
export function terminalSplitHotkeyAxis(
  event: globalThis.KeyboardEvent,
  isMac = isMacPlatform(),
): TerminalSplitAxis | null {
  if (event.isComposing) return null;
  if (matchesShortcut(event, "terminal.splitRight", { mac: isMac })) return "columns";
  if (matchesShortcut(event, "terminal.splitDown", { mac: isMac })) return "rows";
  return null;
}

/** Bind the chord for ``axis`` while mounted. */
export function useTerminalSplitHotkey(
  axis: TerminalSplitAxis,
  onSplit: () => void,
  isMac = isMacPlatform(),
): void {
  const latest = useRef(onSplit);
  latest.current = onSplit;

  useEffect(() => {
    const handler = (event: globalThis.KeyboardEvent): void => {
      if (terminalSplitHotkeyAxis(event, isMac) !== axis || isFocusInDialogOrMenu()) return;
      event.preventDefault();
      event.stopPropagation();
      // Claim but skip auto-repeat: holding the chord would open a shell per tick.
      if (event.repeat) return;
      latest.current();
    };

    // Capture before the focused xterm can forward the chord to its PTY.
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [axis, isMac]);
}
