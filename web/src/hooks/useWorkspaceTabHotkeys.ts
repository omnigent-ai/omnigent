// ⌃1–9 (Alt+1–9 on Win/Linux, where Ctrl+digit opens sidebar sessions) shows
// the Nth fixed tab of the workspace rail from anywhere in the app. Matched on
// e.code, since Alt changes the typed character on macOS. Bind ONCE.

import { type RefObject, useEffect } from "react";

import { isMacPlatform } from "@/lib/hotkeys";

const DIGIT_CODES = [
  "Digit1",
  "Digit2",
  "Digit3",
  "Digit4",
  "Digit5",
  "Digit6",
  "Digit7",
  "Digit8",
  "Digit9",
] as const;

/** Index of the workspace tab a keydown picks, or -1 when it isn't the chord. */
export function workspaceTabIndex(e: globalThis.KeyboardEvent, isMac = isMacPlatform()): number {
  const chord = isMac ? e.ctrlKey && !e.altKey : e.altKey && !e.ctrlKey;
  if (!chord || e.metaKey || e.shiftKey) return -1;
  if (typeof e.getModifierState === "function" && e.getModifierState("AltGraph")) return -1;
  return DIGIT_CODES.indexOf(e.code as (typeof DIGIT_CODES)[number]);
}

/**
 * @param select Shows the tab at an index and reports whether there was one.
 *   Read through a ref so the listener binds once.
 */
export function useWorkspaceTabHotkeys(
  select: RefObject<(index: number) => boolean>,
  isMac = isMacPlatform(),
): void {
  useEffect(() => {
    const handler = (e: globalThis.KeyboardEvent): void => {
      if (e.repeat || e.defaultPrevented) return;
      const index = workspaceTabIndex(e, isMac);
      if (index === -1) return;
      // Alt+digit is a readline argument in a terminal off macOS.
      if (!isMac && document.activeElement?.closest(".xterm")) return;
      if (!select.current(index)) return;
      e.preventDefault();
      e.stopPropagation();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [select, isMac]);
}
