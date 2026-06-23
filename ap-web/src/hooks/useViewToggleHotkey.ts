// Cmd+P (Ctrl on Win/Linux) flips the center surface of a terminal-first
// session between the chat transcript and the live terminal — the keyboard
// accelerator for the Chat/Terminal segmented pill (see AppShell's
// `setView` + TerminalFirstContext). Sibling to usePinnedSessionHotkeys:
// same once-bound, ref-backed, metaKey||ctrlKey shape. Bind ONCE.
//
// Desktop-only, and only live on terminal-first sessions: a browser tab
// reserves Cmd/Ctrl+P for Print, so the hook is inert outside the Electron
// shell (see isNativeShell). On a normal chat session there is no terminal
// center view to toggle, so the key is left untouched there too — we only
// preventDefault when the toggle would actually do something.

import { useEffect, useRef } from "react";
import { isNativeShell } from "@/lib/nativeBridge";
import type { TerminalFirstContextValue } from "@/shell/TerminalFirstContext";

/** The key that toggles the view (with Cmd/Ctrl). Compared case-insensitively
 *  so neither Shift-state nor layout casing matters. */
const VIEW_TOGGLE_KEY = "p";

/**
 * @param ctx The terminal-first view context (AppShell's
 *   `terminalFirstContextValue`), or null when rendered outside the
 *   provider. The hook reads it live through a ref so it always sees the
 *   current view/session without rebinding.
 */
export function useViewToggleHotkey(ctx: TerminalFirstContextValue | null): void {
  // Bound once; the ref keeps the handler reading the live context.
  const latest = useRef(ctx);
  latest.current = ctx;

  useEffect(() => {
    const handler = (e: globalThis.KeyboardEvent): void => {
      // Desktop-only: in a browser tab Cmd/Ctrl+P is Print, which we must
      // not hijack. Only the Electron shell owns it.
      if (!isNativeShell()) return;
      // Cmd/Ctrl, not Alt (Alt+chord is the message hotkey); Shift left alone.
      if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return;
      if (e.key.toLowerCase() !== VIEW_TOGGLE_KEY) return;

      const c = latest.current;
      // Only terminal-first sessions have a center terminal to flip to, and
      // a rail-opened user shell owns the view chrome-free (its own close
      // affordance) — mirror the pill, which hides in both cases.
      if (!c || !c.isTerminalFirst || c.isShellView) return;

      e.preventDefault(); // suppress the Electron print dialog
      c.setView(c.view === "terminal" ? "chat" : "terminal");
    };

    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);
}
