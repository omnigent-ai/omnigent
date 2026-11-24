import { useSyncExternalStore } from "react";
import {
  readTerminalThemeMode,
  resolveTerminalIsDark,
  subscribeTerminalTheme,
  TERMINAL_THEME_DEFAULT,
  type TerminalThemeMode,
} from "@/lib/terminalThemePreferences";
import { useResolvedThemeMode } from "./useResolvedThemeMode";

// The last mode written while anything followed it: a write broadcasts the intended mode even
// when storage refused it, and readers follow the broadcast, not a re-read.
let broadcastMode: TerminalThemeMode | null = null;
let followers = 0;

function followTerminalTheme(listener: () => void): () => void {
  followers += 1;
  const unsubscribe = subscribeTerminalTheme((mode) => {
    broadcastMode = mode;
    listener();
  });
  return () => {
    unsubscribe();
    followers -= 1;
    if (followers === 0) broadcastMode = null;
  };
}

/** The terminal theme setting, live as Settings or ⌘K change it. */
export function useTerminalThemeMode(): TerminalThemeMode {
  return useSyncExternalStore(
    followTerminalTheme,
    () => broadcastMode ?? readTerminalThemeMode(),
    () => TERMINAL_THEME_DEFAULT,
  );
}

/**
 * Whether terminal surfaces render dark. The terminal theme setting is
 * independent of the app's: "auto" follows the app's resolved appearance,
 * while "light"/"dark" pin it. A Settings change re-renders live via the
 * preference's pub/sub.
 */
export function useTerminalIsDark(): boolean {
  const appIsDark = useResolvedThemeMode() === "dark";
  return resolveTerminalIsDark(useTerminalThemeMode(), appIsDark);
}
