// ⌘⌥T (Ctrl+Alt+T on Win/Linux) flips terminal-first sessions between Chat
// and Terminal. It belongs to the same app-level view family as ⌘⌥[ / ⌘⌥].

import { useEffect, useRef } from "react";

import { isIOSShell } from "@/lib/nativeBridge";
import type { TerminalFirstContextValue } from "@/shell/TerminalFirstContext";

const IS_MAC =
  typeof navigator !== "undefined" &&
  /Mac|iPhone|iPad|iPod/i.test(navigator.platform || navigator.userAgent || "");

/** Platform-specific label used by shortcut discovery surfaces. */
export const VIEW_MODE_TOGGLE_SHORTCUT = IS_MAC ? "⌘⌥T" : "Ctrl+Alt+T";

/** Whether the current state has a usable Chat/Terminal destination. */
export function canToggleTerminalFirstView(
  ctx: TerminalFirstContextValue | null,
): ctx is TerminalFirstContextValue {
  if (!ctx || !ctx.isTerminalFirst || ctx.isShellView || isIOSShell()) return false;
  return ctx.view === "terminal" || ctx.terminalsAvailable;
}

/** True for Cmd/Ctrl+Alt+T without Shift, repeat, or AltGraph. */
function isViewModeToggleHotkey(event: globalThis.KeyboardEvent): boolean {
  if (!(event.metaKey || event.ctrlKey) || !event.altKey || event.shiftKey || event.repeat) {
    return false;
  }
  if (typeof event.getModifierState === "function" && event.getModifierState("AltGraph")) {
    return false;
  }
  // Option changes the produced character on macOS, so match the physical key.
  return event.code === "KeyT";
}

/**
 * Bind the terminal-first Chat/Terminal flip once at AppShell.
 *
 * `enabled` is false in embedded mode, where the host page owns global chords.
 */
export function useViewModeToggleHotkey(
  ctx: TerminalFirstContextValue | null,
  enabled = true,
): void {
  const latest = useRef(ctx);
  latest.current = ctx;

  useEffect(() => {
    if (!enabled) return;

    const handler = (event: globalThis.KeyboardEvent): void => {
      if (!isViewModeToggleHotkey(event)) return;
      const current = latest.current;
      if (!canToggleTerminalFirstView(current)) return;

      // Unlike ⌘K, this deliberately fires inside xterm: otherwise users could
      // enter Terminal with the chord but could not use it to return to Chat.
      event.preventDefault();
      event.stopPropagation();
      current.setView(current.view === "chat" ? "terminal" : "chat");
    };

    // Capture before xterm's target listener can stop propagation or forward
    // the chord to the PTY.
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [enabled]);
}
