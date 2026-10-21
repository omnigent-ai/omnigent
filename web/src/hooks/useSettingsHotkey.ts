import { useEffect } from "react";

import { hasCommandModifier, isMacPlatform } from "@/lib/hotkeys";
import { useNavigate } from "@/lib/routing";

/** True for Cmd+Alt+, on Apple platforms or Ctrl+Alt+, elsewhere. */
export function isSettingsHotkey(
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
  return event.code === "Comma";
}

/** Navigate to Settings from anywhere in the app. */
export function useSettingsHotkey(enabled = true, isMac = isMacPlatform()): void {
  const navigate = useNavigate();

  useEffect(() => {
    if (!enabled) return;
    const handler = (event: globalThis.KeyboardEvent): void => {
      if (event.repeat || !isSettingsHotkey(event, isMac)) return;
      event.preventDefault();
      event.stopPropagation();
      navigate("/settings");
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled, isMac, navigate]);
}
