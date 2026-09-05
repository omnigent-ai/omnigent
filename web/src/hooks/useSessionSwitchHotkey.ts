// Cmd+[ / Cmd+] (Ctrl+[ / Ctrl+] on Win/Linux) opens the previous / next
// sidebar session, wrapping at the ends. Sibling to the sidebar-toggle
// (⌘⌥[ / ⌘⌥]) hotkey — they don't collide, that one requires Alt and this one
// requires Alt up. Bind ONCE.
//
// Why brackets: they read as "step back / forward" (matching the browser's
// ⌘[ / ⌘] Back/Forward, which we claim), and unlike the old ⌘↑/↓ they carry no
// text-editing meaning, so the hotkey can fire while the composer is focused
// without stealing a caret-to-start/end. It still bails inside surfaces that
// bind ⌘[ / ⌘] themselves (Monaco outdents/indents; xterm forwards to the PTY).

import { useEffect, useRef } from "react";

import { hasCommandModifier, isMacPlatform } from "@/lib/hotkeys";
import { useNavigate } from "@/lib/routing";

/** Surfaces that keep bracket chords or must not navigate behind an overlay. */
const HOTKEY_OWNING_SURFACES = ".xterm, .monaco-editor, [cmdk-input]";

/**
 * @param orderedIds Conversation ids in sidebar render order, visible sections
 *   only (the rows the user can actually see).
 * @param activeId The open conversation (route param), or undefined off-list
 *   (new-chat / inbox).
 */
export function useSessionSwitchHotkey(
  orderedIds: readonly string[],
  activeId: string | undefined,
  isMac = isMacPlatform(),
): void {
  const navigate = useNavigate();
  // Bound once; the ref keeps the handler reading the live list/route.
  const latest = useRef({ orderedIds, activeId });
  latest.current = { orderedIds, activeId };

  useEffect(() => {
    const handler = (e: globalThis.KeyboardEvent): void => {
      // Ignore auto-repeat: holding the chord would race through sessions.
      if (e.repeat) return;
      // Cmd/Ctrl only — no Alt (that's the sidebar-toggle chord) or Shift.
      if (!hasCommandModifier(e, isMac) || e.altKey || e.shiftKey) return;
      // Match the physical bracket key (e.code is layout/modifier-stable), the
      // same way the sidebar-toggle sibling does.
      const dir = e.code === "BracketRight" ? 1 : e.code === "BracketLeft" ? -1 : 0;
      if (dir === 0) return;

      // Yield when a focused widget claimed the chord before this window listener.
      if (e.defaultPrevented) return;

      // Leave the chord to terminals / the code editor that bind it themselves.
      const el = document.activeElement;
      if (el instanceof Element && el.closest(HOTKEY_OWNING_SURFACES)) return;

      const { orderedIds: ids, activeId: active } = latest.current;
      if (ids.length === 0) return;

      e.preventDefault(); // suppress the browser's ⌘[ / ⌘] Back/Forward gesture
      e.stopPropagation();
      const current = active ? ids.indexOf(active) : -1;
      // Off-list: ] enters at the top, [ at the bottom. Otherwise step + wrap.
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
  }, [navigate, isMac]);
}
