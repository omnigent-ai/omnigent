// Cmd+1..9/0 (Ctrl on Win/Linux) jumps to the Nth session the sidebar lists,
// top to bottom (the caller passes its visible rows): 1–9 → the first nine,
// 0 → the tenth (browser-tab-style mapping). Once-bound, ref-backed, and
// platform-aware (only ⌘ fires on macOS, only Ctrl on Win/Linux). Fires even in
// a focused text field so you can jump mid-compose. Bind ONCE.
//
// Platform-aware chord: a browser tab reserves plain Cmd/Ctrl+digit for native
// tab-switching, so in the browser the binding adds Alt (Cmd/Ctrl+Alt+digit) to
// own a free chord; the Electron shell keeps the plain Cmd/Ctrl+digit it can
// safely claim. With Alt held macOS rewrites e.key to a composed glyph
// (⌥1 → "¡"), so the browser path matches on e.code (physical key) while the
// native path matches on e.key. The shortcuts-dialog row mirrors the same split.

import { useEffect, useRef } from "react";
import { isMacPlatform } from "@/lib/hotkeys";
import { useNavigate } from "@/lib/routing";
import { matchedShortcutChordIndex, SESSION_NUMBER_DIGITS } from "@/lib/shortcuts/registry";

/** Index → the digit that selects it. Shared with the sidebar's row hints so binding and label
 *  can't drift. */
export const SESSION_HOTKEY_DIGITS = SESSION_NUMBER_DIGITS;

/**
 * @param orderedSessionIds Conversation ids the sidebar shows, in render order.
 * @param activeId The open conversation (route param), or undefined off-list.
 */
export function useSessionNumberHotkeys(
  orderedSessionIds: readonly string[],
  activeId: string | undefined,
  isMac = isMacPlatform(),
): void {
  const navigate = useNavigate();
  // Bound once; the ref keeps the handler reading the live list/route.
  const latest = useRef({ orderedSessionIds, activeId });
  latest.current = { orderedSessionIds, activeId };

  useEffect(() => {
    const handler = (e: globalThis.KeyboardEvent): void => {
      // Native: plain Cmd/Ctrl+digit (Alt+chord is the message hotkey). Browser:
      // Cmd/Ctrl+Alt+digit, since plain Cmd/Ctrl+digit is the native tab-switch;
      // AltGr+digit there composes a character instead. Shift is left to other bindings.
      const index = matchedShortcutChordIndex(e, "session.number", { mac: isMac });
      if (index === -1) return;

      const { orderedSessionIds: ids, activeId: active } = latest.current;
      const targetId = ids[index];
      // No session at that slot: leave the native event untouched.
      if (!targetId) return;

      e.preventDefault(); // suppress the browser's native ⌘-digit tab-switch
      if (targetId !== active) navigate(`/c/${targetId}`);
    };

    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [navigate, isMac]);
}
