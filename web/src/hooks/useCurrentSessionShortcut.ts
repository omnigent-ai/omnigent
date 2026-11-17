import { useEffect, useRef } from "react";

import { useIsEmbedded } from "@/lib/embedded";
import { getEmbedRoot } from "@/lib/host";
import { isMacPlatform } from "@/lib/hotkeys";
import { matchesShortcut, type ShortcutId } from "@/lib/shortcuts/registry";

// Surfaces that own their keystrokes, so a session shortcut pressed there is left alone.
const KEY_OWNING_SURFACES =
  '.xterm, .monaco-editor, [role="dialog"], [role="alertdialog"], [role="menu"]';

/**
 * Runs `onTrigger` when `shortcutId`'s chord is pressed anywhere in the app,
 * including a focused composer, but not inside terminals, code editors, or open
 * menus and dialogs. Embedded, only presses from inside the app count.
 */
export function useCurrentSessionShortcut(
  shortcutId: ShortcutId,
  onTrigger: () => void,
  enabled = true,
  isMac = isMacPlatform(),
): void {
  const latest = useRef(onTrigger);
  latest.current = onTrigger;
  const isEmbedded = useIsEmbedded();

  useEffect(() => {
    if (!enabled) return;
    const handler = (event: globalThis.KeyboardEvent): void => {
      if (event.defaultPrevented || event.repeat || event.isComposing) return;
      if (!matchesShortcut(event, shortcutId, { mac: isMac })) return;
      const embedRoot = getEmbedRoot();
      if (isEmbedded && (!embedRoot || !event.composedPath().includes(embedRoot))) return;
      const focused = document.activeElement;
      if (focused instanceof Element && focused.closest(KEY_OWNING_SURFACES)) return;
      event.preventDefault();
      event.stopPropagation();
      latest.current();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [shortcutId, enabled, isMac, isEmbedded]);
}
