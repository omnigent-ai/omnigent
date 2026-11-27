// ⌘⌥A (Ctrl+Alt+A elsewhere) adds a nested agent beside the open session.

import { useEffect, useRef } from "react";

import { useIsEmbedded } from "@/lib/embedded";
import { getEmbedRoot } from "@/lib/host";
import { isFocusInDialogOrMenu } from "@/lib/hotkeys";
import { matchesShortcut } from "@/lib/shortcuts/registry";

/** Run `onAdd` on the add-sibling chord while `enabled`; terminals, editors and dialogs keep it. */
export function useAddSiblingSessionHotkey(onAdd: () => void, enabled: boolean): void {
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
