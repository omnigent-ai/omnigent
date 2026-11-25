import { useEffect, useRef } from "react";
import { GlobeIcon } from "lucide-react";

import { hotkeyAvailability, useHotkeyCommand } from "@/hooks/useHotkeyCommand";
import { isMacPlatform } from "@/lib/hotkeys";
import { matchesShortcut } from "@/shell/workspaceTabs/workspaceSidebarShortcuts";

// Terminals and code editors bind their own chords, so they keep this one.
const TEXT_ENTRY_SURFACE = ".xterm, .monaco-editor, .cm-editor";

/** True for Cmd+Alt+B on Apple platforms or Ctrl+Alt+B elsewhere. */
export function isNewBrowserHotkey(
  event: globalThis.KeyboardEvent,
  isMac = isMacPlatform(),
): boolean {
  return matchesShortcut(event, "browser.new", { mac: isMac });
}

/** Bind the new-browser-tab shortcut while the workspace supports Browser. */
export function useNewBrowserHotkey(
  onOpen: () => void,
  enabled = true,
  isMac = isMacPlatform(),
): void {
  const latest = useRef(onOpen);
  latest.current = onOpen;
  useHotkeyCommand("browser.new", {
    icon: GlobeIcon,
    keywords: ["browser", "web", "tab", "url", "preview"],
    run: () => latest.current(),
    availability: hotkeyAvailability(enabled),
    // The new browser tab takes focus once the palette has closed.
    movesFocus: true,
  });

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
