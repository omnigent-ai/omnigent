// Whether the terminal extra-keys row should render. Touch-based, never
// width-based: a phone, a foldable at any width, or an iPad all qualify; a
// desktop browser never does. The Settings preference overrides both ways.

import { useSyncExternalStore } from "react";
import { isAndroidShell, isIOSShell } from "@/lib/nativeBridge";
import {
  TERMINAL_EXTRA_KEYS_DEFAULT,
  readTerminalExtraKeysMode,
  subscribeTerminalExtraKeys,
} from "@/lib/terminalExtraKeysPreferences";

const COARSE_POINTER_QUERY = "(pointer: coarse)";

function subscribeCoarsePointer(callback: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  const mql = window.matchMedia(COARSE_POINTER_QUERY);
  mql.addEventListener("change", callback);
  return () => mql.removeEventListener("change", callback);
}

function readCoarsePointer(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia(COARSE_POINTER_QUERY).matches;
}

/**
 * True when the extra-keys row belongs under this terminal.
 *
 * `!readOnly && (pref === "on" || (pref === "auto" && touchCapable))`, where
 * touch-capable is the native iOS/Android shell (touch by construction, even
 * with a trackpad or keyboard attached) or a coarse primary pointer.
 */
export function useTerminalExtraKeysVisibility(readOnly: boolean): boolean {
  const pref = useSyncExternalStore(
    subscribeTerminalExtraKeys,
    readTerminalExtraKeysMode,
    () => TERMINAL_EXTRA_KEYS_DEFAULT,
  );
  const coarsePointer = useSyncExternalStore(
    subscribeCoarsePointer,
    readCoarsePointer,
    () => false,
  );
  if (readOnly) return false;
  if (pref === "on") return true;
  if (pref === "off") return false;
  return isIOSShell() || isAndroidShell() || coarsePointer;
}
