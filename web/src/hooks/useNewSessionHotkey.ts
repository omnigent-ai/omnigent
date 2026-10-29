import { useEffect } from "react";

import { useIsEmbedded } from "@/lib/embedded";
import { getEmbedRoot } from "@/lib/host";
import { hasCommandModifier, isMacPlatform } from "@/lib/hotkeys";
import { isNativeShell } from "@/lib/nativeBridge";
import { useNavigate } from "@/lib/routing";

/** Browser Cmd/Ctrl+Shift+O; native shells Cmd/Ctrl+N, with the older +Alt chord kept. */
export function isNewSessionHotkey(
  e: globalThis.KeyboardEvent,
  isMac = isMacPlatform(),
  native = isNativeShell(),
): boolean {
  if (!hasCommandModifier(e, isMac) || e.getModifierState("AltGraph")) return false;
  if (!native) return !e.altKey && e.shiftKey && e.key.toLowerCase() === "o";
  // The key that types N, on any layout. Option changes that character on macOS, so the
  // older Alt chord keeps matching the physical key.
  return !e.shiftKey && (e.altKey ? e.code === "KeyN" : e.key.toLowerCase() === "n");
}

/** Navigate to the same new-session route used by the command palette. */
export function useNewSessionHotkey(enabled = true, isMac = isMacPlatform()): void {
  const navigate = useNavigate();
  const isEmbedded = useIsEmbedded();

  useEffect(() => {
    if (!enabled) return;
    const handler = (e: globalThis.KeyboardEvent): void => {
      if (e.repeat || !isNewSessionHotkey(e, isMac)) return;
      const embedRoot = getEmbedRoot();
      if (isEmbedded && (!embedRoot || !e.composedPath().includes(embedRoot))) return;
      e.preventDefault();
      e.stopPropagation();
      navigate("/");
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled, isMac, isEmbedded, navigate]);
}
