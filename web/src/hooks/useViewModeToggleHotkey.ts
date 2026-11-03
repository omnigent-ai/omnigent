import { useEffect, useRef } from "react";

import { hasCommandModifier, isMacPlatform } from "@/lib/hotkeys";

type ViewMode = "chat" | "terminal";

interface ViewModeToggleHotkeyOptions {
  enabled: boolean;
  view: ViewMode;
  setView: (view: ViewMode) => void;
}

/** True for Cmd+Alt+\ on Apple platforms or Ctrl+Alt+\ elsewhere. */
export function isViewModeToggleHotkey(
  event: globalThis.KeyboardEvent,
  isMac = isMacPlatform(),
): boolean {
  if (!hasCommandModifier(event, isMac) || !event.altKey || event.shiftKey || event.isComposing) {
    return false;
  }
  if (typeof event.getModifierState === "function" && event.getModifierState("AltGraph")) {
    return false;
  }
  return event.code === "Backslash";
}

export function useViewModeToggleHotkey(
  options: ViewModeToggleHotkeyOptions,
  isMac = isMacPlatform(),
): void {
  const latest = useRef(options);
  latest.current = options;

  useEffect(() => {
    if (!options.enabled) return;

    const handler = (event: globalThis.KeyboardEvent): void => {
      if (event.repeat || !isViewModeToggleHotkey(event, isMac)) return;
      event.preventDefault();
      event.stopPropagation();
      const { view, setView } = latest.current;
      setView(view === "chat" ? "terminal" : "chat");
    };

    // Capture before xterm can forward the chord into the active terminal.
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [isMac, options.enabled]);
}
