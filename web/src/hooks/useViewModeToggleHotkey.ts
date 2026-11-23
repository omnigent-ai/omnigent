import { useEffect, useRef } from "react";

import { isMacPlatform } from "@/lib/hotkeys";
import { matchesShortcut } from "@/lib/shortcuts/registry";
import { useBindShortcutTarget } from "@/lib/shortcuts/shortcutTargets";

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
  return !event.isComposing && matchesShortcut(event, "view.mode", { mac: isMac });
}

export function useViewModeToggleHotkey(
  options: ViewModeToggleHotkeyOptions,
  isMac = isMacPlatform(),
): void {
  const latest = useRef(options);
  latest.current = options;
  // The view the chord switches to now, so only that view's menu row shows it.
  useBindShortcutTarget(
    "view.mode",
    options.view === "chat" ? "terminal" : "chat",
    options.enabled,
  );

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
