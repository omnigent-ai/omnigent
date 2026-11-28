// Cmd/Ctrl+1..9 jumps to the Nth listed session, even from a text field; Cmd+0 stays Actual Size.
// A browser adds Alt (plain Cmd/Ctrl+digit switches tabs) and matches e.code, since Alt composes
// e.key on macOS. Bind once; registry.ts owns the chords.

import { useEffect, useRef } from "react";
import { isMacPlatform } from "@/lib/hotkeys";
import { useNavigate, useRebasePath } from "@/lib/routing";
import { sessionIdAtPath } from "@/lib/sessionRoutes";
import { matchedShortcutChordIndex, SESSION_NUMBER_DIGITS } from "@/lib/shortcuts/registry";

/** Index → the digit that selects it. Shared with the sidebar's row hints so binding and label
 *  can't drift. */
export const SESSION_HOTKEY_DIGITS = SESSION_NUMBER_DIGITS;

/**
 * @param orderedSessionIds Conversation ids the sidebar shows, in render order.
 */
export function useSessionNumberHotkeys(
  orderedSessionIds: readonly string[],
  isMac = isMacPlatform(),
): void {
  const navigate = useNavigate();
  const rebasePath = useRebasePath();
  // Bound once; the ref keeps the handler reading the live list and mount.
  const latest = useRef({ orderedSessionIds, rebasePath });
  latest.current = { orderedSessionIds, rebasePath };

  useEffect(() => {
    const handler = (e: globalThis.KeyboardEvent): void => {
      // Native: plain Cmd/Ctrl+digit (Alt+chord is the message hotkey). Browser:
      // Cmd/Ctrl+Alt+digit, since plain Cmd/Ctrl+digit is the native tab-switch;
      // AltGr+digit there composes a character instead. Shift is left to other bindings.
      const index = matchedShortcutChordIndex(e, "session.number", { mac: isMac });
      if (index === -1) return;

      const { orderedSessionIds: ids, rebasePath: rebase } = latest.current;
      const targetId = ids[index];
      // No session at that slot: leave the native event untouched.
      if (!targetId) return;

      e.preventDefault(); // suppress the browser's native ⌘-digit tab-switch
      // The URL, not the rendered route: right after leaving a session the router still renders
      // it while the next page loads, and its digit must still go back to it.
      if (sessionIdAtPath(window.location.pathname, rebase) !== targetId) {
        navigate(`/c/${targetId}`);
      }
    };

    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [navigate, isMac]);
}
