// Cmd+Alt+1..9 (Ctrl+Alt on Win/Linux) jumps to the Nth visible sidebar
// session. Browser-safe: the plain Cmd/Ctrl+digit chord is reserved by browsers
// for tab-switching (which is why usePinnedSessionHotkeys is Electron-only), so
// adding Alt frees a binding the page can own in the browser AND the desktop
// app, and it stays in the existing Cmd+Alt family (message nav is Cmd+Alt+↑/↓).
// Sibling to useSessionSwitchHotkey — same once-bound, ref-backed shape; fires
// even in a focused text field so you can jump mid-compose. Bind ONCE.

import { useEffect, useRef } from "react";
import { useNavigate } from "@/lib/routing";

// Match on KeyboardEvent.code (physical key), NOT .key: with Alt/Option held,
// macOS rewrites .key to a composed glyph (Option+1 → "¡"), but .code stays
// "Digit1". Index i → the i-th visible session (1-based key, 0-based array).
export const DIGIT_SESSION_HOTKEY_CODES = [
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

/**
 * @param orderedIds Conversation ids in sidebar render order, visible sections
 *   only (same source as useSessionSwitchHotkey).
 * @param activeId The open conversation (route param), or undefined off-list.
 */
export function useDigitSessionHotkey(
  orderedIds: readonly string[],
  activeId: string | undefined,
): void {
  const navigate = useNavigate();
  // Bound once; the ref keeps the handler reading the live list/route.
  const latest = useRef({ orderedIds, activeId });
  latest.current = { orderedIds, activeId };

  useEffect(() => {
    const handler = (e: globalThis.KeyboardEvent): void => {
      // Require Cmd/Ctrl AND Alt (the browser owns plain Cmd/Ctrl+digit);
      // Shift is left to other bindings.
      if (!(e.metaKey || e.ctrlKey) || !e.altKey || e.shiftKey) return;

      const index = DIGIT_SESSION_HOTKEY_CODES.indexOf(
        e.code as (typeof DIGIT_SESSION_HOTKEY_CODES)[number],
      );
      if (index === -1) return;

      const { orderedIds: ids, activeId: active } = latest.current;
      const targetId = ids[index];
      // No session at that slot: leave the event untouched.
      if (!targetId) return;

      e.preventDefault();
      if (targetId !== active) navigate(`/c/${targetId}`);
    };

    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [navigate]);
}
