// ⌘[ / ⌘] (Ctrl+[ / Ctrl+] on Win/Linux), the desktop Go menu, trackpad swipes,
// and the mouse back/forward buttons step back and forward through in-app
// navigation history. Bind ONCE.

import { useCallback, useEffect, useState } from "react";

import { useDesktopCommand } from "@/lib/desktopCommands";
import { useIsEmbedded } from "@/lib/embedded";
import { hasCommandModifier, isMacPlatform } from "@/lib/hotkeys";
import { isElectronShell } from "@/lib/nativeBridge";
import { useNavigate } from "@/lib/routing";

/** Surfaces that keep bracket chords (Monaco outdent/indent, the PTY, the palette). */
const BRACKET_OWNING_SURFACES = ".xterm, .monaco-editor, [cmdk-input]";

/** React Router's position in the history stack. */
function historyIndex(): number {
  const index = (window.history.state as { idx?: unknown } | null)?.idx;
  return typeof index === "number" ? index : 0;
}

/**
 * The first in-app history entry: 0 standalone. Embedded, the host's router owns
 * the history and its own pages come first, so it is the entry the app opened on.
 */
let firstAppEntry = 0;

export function useHistoryNavigation(isMac = isMacPlatform()): void {
  const navigate = useNavigate();
  const embedded = useIsEmbedded();
  // Set during the first render, before anything below reads it.
  useState(() => {
    firstAppEntry = embedded ? historyIndex() : 0;
  });
  // Back stops at the first in-app entry rather than leaving for a sign-in or setup page.
  const go = useCallback(
    (delta: -1 | 1) => {
      if (delta === -1 && historyIndex() <= firstAppEntry) return;
      void navigate(delta);
    },
    [navigate],
  );

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.repeat || event.defaultPrevented || event.altKey || event.shiftKey) return;
      if (!hasCommandModifier(event, isMac)) return;
      const delta = event.code === "BracketLeft" ? -1 : event.code === "BracketRight" ? 1 : 0;
      if (delta === 0) return;
      const focused = document.activeElement;
      if (focused instanceof Element && focused.closest(BRACKET_OWNING_SURFACES)) return;
      event.preventDefault();
      go(delta);
    };
    // Chromium already maps the mouse back/forward buttons to history; taking them here
    // also stops them at the first in-app entry. Browser tabs keep the browser's handling.
    const onMouseUp = (event: MouseEvent) => {
      const delta = event.button === 3 ? -1 : event.button === 4 ? 1 : 0;
      if (delta === 0) return;
      event.preventDefault();
      go(delta);
    };
    const electron = isElectronShell();
    window.addEventListener("keydown", onKeyDown);
    if (electron) window.addEventListener("mouseup", onMouseUp);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      if (electron) window.removeEventListener("mouseup", onMouseUp);
    };
  }, [go, isMac]);

  useDesktopCommand("go-back", () => go(-1));
  useDesktopCommand("go-forward", () => go(1));
}
