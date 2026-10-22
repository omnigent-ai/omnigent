// ⌘⌥T (Ctrl+Alt+T on Win/Linux) opens a new shell in the workspace rail,
// launching the remembered default type — the keyboard path for the tab-strip
// "+" menu, whose row launches on click (mouse-only otherwise). Sibling to the
// other app-shell hotkeys (⌘K palette, ⌘⌥[ / ⌘⌥] sidebars); like them it's
// bound ONCE at the app shell, where the shell-launch wiring lives.
//
// Why ⌘⌥T: it joins the app's ⌘⌥ family (sidebar toggles, voice) with no
// browser/OS clash — unlike ⌘` (macOS window cycling) or ⌘⇧T (browser reopen-
// tab). "T" for terminal.

import { useEffect, useRef } from "react";

/** Editor/terminal surfaces that own their own keystrokes; the chord defers. */
const TEXT_ENTRY_SURFACE = ".monaco-editor, .xterm";

function isMacPlatform(): boolean {
  if (typeof navigator === "undefined") return false;
  const uaData = (navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData;
  const platform = uaData?.platform ?? navigator.platform ?? navigator.userAgent ?? "";
  return /Mac|iPhone|iPad|iPod/i.test(platform);
}

/** True for Cmd+Alt+T on Apple platforms or Ctrl+Alt+T elsewhere. */
export function isNewShellHotkey(e: globalThis.KeyboardEvent, isMac = isMacPlatform()): boolean {
  const platformModifier = isMac ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey;
  if (!platformModifier || !e.altKey || e.shiftKey) return false;
  // AltGr reports as Ctrl+Alt on Windows/Linux, so an ordinary AltGr+T
  // keystroke on an international layout would otherwise match this chord (and
  // get swallowed). Bail so intl typing never launches a shell — same guard the
  // sibling hotkeys carry.
  if (typeof e.getModifierState === "function" && e.getModifierState("AltGraph")) return false;
  // Match the physical key: Alt remaps the character on many layouts (e.g.
  // ⌥T is "†" on macOS), so keying off e.key would miss the chord.
  return e.code === "KeyT";
}

/**
 * Bind ⌘/Ctrl+Alt+T to open a new shell. Bind ONCE at the app shell.
 *
 * The chord defers to a focused Monaco editor or xterm terminal — those
 * surfaces own their keystrokes, and stealing the chord mid-edit would surprise
 * the user. It fires only when ``enabled`` (the caller gates on the session
 * declaring shell access and being reachable).
 *
 * @param onLaunch Open the default shell (no-op if the caller can't right now).
 * @param enabled  Pass ``false`` to disable (no session, no shell access, or an
 *   offline session the browser can't reconnect). Defaults to enabled.
 */
export function useNewShellHotkey(
  onLaunch: () => void,
  enabled = true,
  isMac = isMacPlatform(),
): void {
  // Held in a ref so the bound handler always calls the latest closure without
  // re-registering on every render.
  const latest = useRef(onLaunch);
  latest.current = onLaunch;

  useEffect(() => {
    if (!enabled) return;
    const handler = (e: globalThis.KeyboardEvent): void => {
      // Ignore auto-repeat: holding the chord would spawn a shell per tick.
      if (e.repeat || !isNewShellHotkey(e, isMac)) return;
      // Leave the chord to a focused editor/terminal that consumes keystrokes.
      const el = document.activeElement;
      if (el instanceof Element && el.closest(TEXT_ENTRY_SURFACE) !== null) return;
      e.preventDefault();
      e.stopPropagation();
      latest.current();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled, isMac]);
}
