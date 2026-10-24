import { useEffect } from "react";

import { useIsEmbedded } from "@/lib/embedded";
import { getEmbedRoot } from "@/lib/host";
import { hasCommandModifier, isMacPlatform } from "@/lib/hotkeys";
import { isNativeShell } from "@/lib/nativeBridge";
import { useNavigate } from "@/lib/routing";

/** Browser Cmd/Ctrl+Shift+O; preserve the native Cmd/Ctrl+Alt+N chord. */
export function isNewSessionHotkey(
  e: globalThis.KeyboardEvent,
  isMac = isMacPlatform(),
  native = isNativeShell(),
): boolean {
  if (!hasCommandModifier(e, isMac) || e.getModifierState("AltGraph")) return false;
  return native
    ? e.altKey && !e.shiftKey && e.code === "KeyN"
    : !e.altKey && e.shiftKey && e.key.toLowerCase() === "o";
}

/** Start a new session via `onNewSession`, or open the plain composer. */
export function useNewSessionHotkey(
  enabled = true,
  isMac = isMacPlatform(),
  onNewSession?: () => void,
): void {
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
      if (onNewSession) onNewSession();
      else navigate("/");
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled, isMac, isEmbedded, navigate, onNewSession]);
}
