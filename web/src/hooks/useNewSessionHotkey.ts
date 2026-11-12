import { useEffect } from "react";

import { useIsEmbedded } from "@/lib/embedded";
import { getEmbedRoot } from "@/lib/host";
import { hasCommandModifier, isMacPlatform } from "@/lib/hotkeys";
import { isNativeShell } from "@/lib/nativeBridge";
import { useNavigate } from "@/lib/routing";

/** Router state for the new-session route asking its composer to take focus. */
export const NEW_SESSION_COMPOSER_FOCUS_STATE = { focusComposer: true } as const;

/** Whether a navigation to the new-session page asked its composer to take focus. */
export function requestsComposerFocus(state: unknown): boolean {
  return (
    typeof state === "object" &&
    state !== null &&
    (state as { focusComposer?: unknown }).focusComposer === true
  );
}

/** Browser Cmd/Ctrl+Shift+O; native shells Cmd/Ctrl+N. */
export function isNewSessionHotkey(
  e: globalThis.KeyboardEvent,
  isMac = isMacPlatform(),
  native = isNativeShell(),
): boolean {
  if (!hasCommandModifier(e, isMac) || e.getModifierState("AltGraph")) return false;
  if (!native) return !e.altKey && e.shiftKey && e.key.toLowerCase() === "o";
  // The key that types N, on any layout.
  return !e.shiftKey && !e.altKey && e.key.toLowerCase() === "n";
}

/** Navigate to the new-session route used by the command palette, landing focus in its composer. */
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
      navigate("/", { state: NEW_SESSION_COMPOSER_FOCUS_STATE });
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [enabled, isMac, isEmbedded, navigate]);
}
