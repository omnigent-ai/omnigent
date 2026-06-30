// Cmd+↑/↓ (Ctrl+↑/↓ on Win/Linux) opens the previous / next sidebar session,
// wrapping at the ends. Sibling to ChatPage's Cmd+Alt+↑/↓ message nav; they
// don't collide (that one requires Alt, this one requires Alt up). Fires even
// in a focused text field so you can switch mid-compose. Bind ONCE.

import { useEffect, useRef } from "react";
import { useNavigate } from "@/lib/routing";

/**
 * @param orderedIds Conversation ids in sidebar render order, visible sections
 *   only (the rows the user can actually see).
 * @param activeId The open conversation (route param), or undefined off-list
 *   (new-chat / inbox).
 */
export function useSessionSwitchHotkey(
  orderedIds: readonly string[],
  activeId: string | undefined,
): void {
  const navigate = useNavigate();
  // Bound once; the ref keeps the handler reading the live list/route.
  const latest = useRef({ orderedIds, activeId });
  latest.current = { orderedIds, activeId };

  useEffect(() => {
    const handler = (e: globalThis.KeyboardEvent): void => {
      // Cmd/Ctrl, not Alt (Alt+arrow is the message hotkey); Shift left to selection.
      if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return;
      if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;

      const { orderedIds: ids, activeId: active } = latest.current;
      if (ids.length === 0) return;

      e.preventDefault(); // also suppresses the native caret-to-start/end in fields
      const dir = e.key === "ArrowDown" ? 1 : -1;
      const current = active ? ids.indexOf(active) : -1;
      // Off-list: ↓ enters at the top, ↑ at the bottom. Otherwise step + wrap.
      const next =
        current === -1
          ? dir === 1
            ? 0
            : ids.length - 1
          : (current + dir + ids.length) % ids.length;

      const nextId = ids[next];
      if (nextId && nextId !== active) navigate(`/c/${nextId}`);
    };

    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [navigate]);
}
