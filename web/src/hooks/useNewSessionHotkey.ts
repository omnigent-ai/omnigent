import { useEffect } from "react";

import { useIsEmbedded } from "@/lib/embedded";
import { getEmbedRoot } from "@/lib/host";
import { isMacPlatform } from "@/lib/hotkeys";
import { type Chord, chordMatches } from "@/lib/shortcuts/chord";
import { isNativeShell } from "@/lib/nativeBridge";
import { useNavigate } from "@/lib/routing";

const BROWSER_NEW_SESSION_CHORD: Chord = { mod: true, shift: true, key: "o" };
// The key that types N, on any layout.
const NATIVE_NEW_SESSION_CHORD: Chord = { mod: true, key: "n" };

/** Browser Cmd/Ctrl+Shift+O; native shells Cmd/Ctrl+N. */
export function isNewSessionHotkey(
  e: globalThis.KeyboardEvent,
  isMac = isMacPlatform(),
  native = isNativeShell(),
): boolean {
  return chordMatches(native ? NATIVE_NEW_SESSION_CHORD : BROWSER_NEW_SESSION_CHORD, e, isMac);
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
