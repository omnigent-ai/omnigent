import { useEffect } from "react";

import { hasCommandModifier, isMacPlatform } from "@/lib/hotkeys";
import { useNavigate } from "@/lib/routing";

/** True for Cmd+N on Apple platforms or Ctrl+N elsewhere, without extra modifiers. */
export function isNewSessionHotkey(e: globalThis.KeyboardEvent, isMac = isMacPlatform()): boolean {
  if (!hasCommandModifier(e, isMac) || e.altKey || e.shiftKey || e.getModifierState("AltGraph")) {
    return false;
  }
  return e.key === "n" || e.key === "N";
}

/** Navigate to the same new-session route used by the command palette. */
export function useNewSessionHotkey(enabled = true, isMac = isMacPlatform()): void {
  const navigate = useNavigate();

  useEffect(() => {
    if (!enabled) return;
    const handler = (e: globalThis.KeyboardEvent): void => {
      if (e.repeat || !isNewSessionHotkey(e, isMac)) return;
      e.preventDefault();
      e.stopPropagation();
      navigate("/");
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled, isMac, navigate]);
}
