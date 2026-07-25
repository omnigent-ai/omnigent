// Cmd+1..9/0 (Ctrl on Win/Linux) jumps to the Nth pinned sidebar session:
// 1–9 → the first nine, 0 → the tenth (browser-tab-style mapping). Sibling to
// useSessionSwitchHotkey — same once-bound, ref-backed, metaKey||ctrlKey shape.
// Fires even in a focused text field so you can jump mid-compose. Bind ONCE.
//
// Platform-aware chord: a browser tab reserves plain Cmd/Ctrl+digit for native
// tab-switching, so in the browser the binding adds Alt (Cmd/Ctrl+Alt+digit) to
// own a free chord; the Electron shell keeps the plain Cmd/Ctrl+digit it can
// safely claim. With Alt held macOS rewrites e.key to a composed glyph
// (⌥1 → "¡"), so the browser path matches on e.code (physical key) while the
// native path matches on e.key. The shortcuts-dialog row mirrors the same split.

import { useEffect, useRef } from "react";
import { useNavigate } from "@/lib/routing";
import { isNativeShell } from "@/lib/nativeBridge";

/** Index → the digit key that selects it (native path; matched against e.key).
 *  Single source of truth shared with the shortcuts dialog so binding and label
 *  can't drift. */
export const PINNED_HOTKEY_DIGITS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"] as const;

/** Index → the physical key code (browser path; matched against e.code, since
 *  Alt rewrites e.key on macOS). Same order as {@link PINNED_HOTKEY_DIGITS}. */
export const PINNED_HOTKEY_CODES = [
  "Digit1",
  "Digit2",
  "Digit3",
  "Digit4",
  "Digit5",
  "Digit6",
  "Digit7",
  "Digit8",
  "Digit9",
  "Digit0",
] as const;

/**
 * @param orderedPinnedIds Pinned conversation ids in sidebar render order
 *   (empty when the Pinned section is collapsed or there are no pins).
 * @param activeId The open conversation (route param), or undefined off-list.
 */
export function usePinnedSessionHotkeys(
  orderedPinnedIds: readonly string[],
  activeId: string | undefined,
): void {
  const navigate = useNavigate();
  // Bound once; the ref keeps the handler reading the live list/route.
  const latest = useRef({ orderedPinnedIds, activeId });
  latest.current = { orderedPinnedIds, activeId };

  useEffect(() => {
    const handler = (e: globalThis.KeyboardEvent): void => {
      // Cmd/Ctrl required; Shift left to other bindings.
      if (e.shiftKey || !(e.metaKey || e.ctrlKey)) return;

      let index: number;
      if (isNativeShell()) {
        // Desktop owns plain Cmd/Ctrl+digit (Alt+chord is the message hotkey).
        if (e.altKey) return;
        index = PINNED_HOTKEY_DIGITS.indexOf(e.key as (typeof PINNED_HOTKEY_DIGITS)[number]);
      } else {
        // Browser: plain Cmd/Ctrl+digit is the native tab-switch, so own the
        // Cmd/Ctrl+Alt+digit chord instead. Alt rewrites e.key → match e.code.
        if (!e.altKey) return;
        // AltGr reports as Ctrl+Alt on Windows/Linux intl layouts — typing
        // AltGr+digit must compose the character, not yank the user to a
        // pinned session. Same guard (and same feature-detect, so synthetic
        // events without the method don't throw) as useSidebarToggleHotkeys.
        if (typeof e.getModifierState === "function" && e.getModifierState("AltGraph")) return;
        index = PINNED_HOTKEY_CODES.indexOf(e.code as (typeof PINNED_HOTKEY_CODES)[number]);
      }
      if (index === -1) return;

      const { orderedPinnedIds: ids, activeId: active } = latest.current;
      const targetId = ids[index];
      // No pinned session at that slot: leave the native event untouched.
      if (!targetId) return;

      e.preventDefault(); // suppress the browser's native ⌘-digit tab-switch
      if (targetId !== active) navigate(`/c/${targetId}`);
    };

    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [navigate]);
}
