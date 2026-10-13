import { useEffect, useRef } from "react";

import { hasCommandModifier, isMacPlatform } from "@/lib/hotkeys";

const TEXT_ENTRY_SURFACE = ".monaco-editor, .xterm";

/** True for Cmd+Alt+B on Apple platforms or Ctrl+Alt+B elsewhere. */
export function isNewBrowserHotkey(
  event: globalThis.KeyboardEvent,
  isMac = isMacPlatform(),
): boolean {
  if (
    !hasCommandModifier(event, isMac) ||
    !event.altKey ||
    event.shiftKey ||
    event.getModifierState("AltGraph")
  ) {
    return false;
  }
  return event.code === "KeyB";
}

/** Bind the new-browser-tab shortcut while the workspace supports Browser. */
export function useNewBrowserHotkey(
  onOpen: () => void,
  enabled = true,
  isMac = isMacPlatform(),
): void {
  const latest = useRef(onOpen);
  latest.current = onOpen;

  useEffect(() => {
    if (!enabled) return;
    const handler = (event: globalThis.KeyboardEvent): void => {
      if (event.repeat || !isNewBrowserHotkey(event, isMac)) return;
      const active = document.activeElement;
      if (active instanceof Element && active.closest(TEXT_ENTRY_SURFACE) !== null) return;
      event.preventDefault();
      event.stopPropagation();
      latest.current();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled, isMac]);
}
