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
    // The key of the chord last handled, so holding it doesn't leak repeats (⌃3 is
    // Escape in a terminal) without picking the tab again.
    let held: string | null = null;
    const handler = (e: globalThis.KeyboardEvent): void => {
      if (e.defaultPrevented) return;
      if (e.repeat) {
        if (held === e.code && workspaceTabIndex(e, isMac) !== -1) {
          e.preventDefault();
          e.stopPropagation();
        }
        return;
      }
      held = null;
      const index = workspaceTabIndex(e, isMac);
      if (index === -1) return;
      // Alt+digit is a readline argument in a terminal off macOS.
      if (!isMac && document.activeElement?.closest(".xterm")) return;
      if (!select.current(index)) return;
      held = e.code;
      e.preventDefault();
      e.stopPropagation();
    };
    const release = (e: globalThis.KeyboardEvent): void => {
      if (e.code === held) held = null;
    };
    // Capture phase, ahead of fields that stop their keys (the project picker's
    // search) and of a focused terminal, which turns ⌃3–⌃8 into control bytes.
    window.addEventListener("keydown", handler, true);
    window.addEventListener("keyup", release, true);
    return () => {
      window.removeEventListener("keydown", handler, true);
      window.removeEventListener("keyup", release, true);
    };
  }, [select, isMac]);
}
