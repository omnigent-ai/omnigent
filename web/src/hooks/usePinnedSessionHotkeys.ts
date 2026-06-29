// Cmd+1..9/0 (Ctrl on Win/Linux) jumps to the Nth pinned sidebar session:
// 1–9 → the first nine, 0 → the tenth (browser-tab-style mapping). Sibling to
// useSessionSwitchHotkey — same once-bound, ref-backed, metaKey||ctrlKey shape.
// Fires even in a focused text field so you can jump mid-compose. Bind ONCE.
//
// Desktop-only: a browser tab reserves Cmd/Ctrl+digit for tab-switching, so the
// hook is inert outside the Electron shell (see isNativeShell). The matching
// per-row chips and the shortcuts-dialog row are gated the same way.

import { useEffect, useRef } from "react";
import { useNavigate } from "@/lib/routing";
import { isNativeShell } from "@/lib/nativeBridge";

/** Index → the digit key that selects it. Single source of truth shared with
 *  the sidebar's per-row shortcut chips so the binding and label can't drift. */
export const PINNED_HOTKEY_DIGITS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"] as const;

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
      // Desktop-only: in a browser tab Cmd/Ctrl+digit is the native
      // tab-switch, which we must not hijack. Only the Electron shell owns it.
      if (!isNativeShell()) return;
      // Cmd/Ctrl, not Alt (Alt+chord is the message hotkey); Shift left alone.
      if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return;

      const index = PINNED_HOTKEY_DIGITS.indexOf(e.key as (typeof PINNED_HOTKEY_DIGITS)[number]);
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
