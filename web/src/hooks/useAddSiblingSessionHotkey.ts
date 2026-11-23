// ⌘⌥A (Ctrl+Alt+A elsewhere) adds a nested agent beside the open session.

import { useEffect, useRef } from "react";

import { useIsEmbedded } from "@/lib/embedded";
import { getEmbedRoot } from "@/lib/host";
import { isFocusInDialogOrMenu } from "@/lib/hotkeys";
import { matchesShortcut } from "@/lib/shortcuts/registry";
import { useBindShortcutTarget } from "@/lib/shortcuts/shortcutTargets";

/**
 * Run `onAdd` on the add-sibling chord while `enabled`; terminals, editors and dialogs keep it.
 * `sessionId` is the session it adds beside, so menus show ⌘⌥A only on that session's rows.
 */
export function useAddSiblingSessionHotkey(
  onAdd: () => void,
  enabled: boolean,
  sessionId?: string,
): void {
  useBindShortcutTarget("session.addSibling", sessionId, enabled);
  const latest = useRef(onAdd);
  latest.current = onAdd;
  const isEmbedded = useIsEmbedded();

  useEffect(() => {
    if (!enabled) return;
    const handler = (event: globalThis.KeyboardEvent) => {
      if (event.defaultPrevented || event.repeat || event.isComposing) return;
      if (!matchesShortcut(event, "session.addSibling")) return;
      const embedRoot = getEmbedRoot();
      if (isEmbedded && (!embedRoot || !event.composedPath().includes(embedRoot))) return;
      if (isFocusInDialogOrMenu() || document.activeElement?.closest(".xterm, .monaco-editor")) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      latest.current();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled, isEmbedded]);
}
