// ⌃` (Ctrl+` on Win/Linux) opens a new shell in the workspace rail, launching
// the remembered default type — the keyboard path for the tab-strip "+" menu,
// whose row launches on click. Bound ONCE at the app shell, where the
// shell-launch wiring lives.
//
// ⌃` is the terminal chord most editors use. It is literal Control on every
// platform, so it never meets macOS window cycling (⌘`), and it is no editing
// key in a terminal or editor, so it also works from a focused shell.

import { useEffect, useRef } from "react";

/** True for ⌃` — Control plus the key left of 1, with no other modifier. */
export function isTerminalHotkey(e: globalThis.KeyboardEvent): boolean {
  return e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey && e.code === "Backquote";
}

/**
 * Bind ⌃` to open a new shell. Bind ONCE at the app shell. It fires only when
 * `enabled` (the caller gates on the session declaring shell access and being
 * reachable).
 *
 * @param onLaunch Open the default shell (no-op if the caller can't right now).
 * @param enabled  Pass `false` to disable (no session, no shell access, or an
 *   offline session the browser can't reconnect). Defaults to enabled.
 */
export function useNewShellHotkey(onLaunch: () => void, enabled = true): void {
  // Held in a ref so the bound handler always calls the latest closure without
  // re-registering on every render.
  const latest = useRef(onLaunch);
  latest.current = onLaunch;

  useEffect(() => {
    if (!enabled) return;
    const handler = (e: globalThis.KeyboardEvent): void => {
      // Ignore auto-repeat: holding the chord would spawn a shell per tick.
      if (e.repeat || !isTerminalHotkey(e)) return;
      e.preventDefault();
      e.stopPropagation();
      latest.current();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled]);
}
