import { useEffect, useState } from "react";
import {
  readTerminalThemeMode,
  resolveTerminalIsDark,
  subscribeTerminalTheme,
  type TerminalThemeMode,
} from "@/lib/terminalThemePreferences";
import { useResolvedThemeMode } from "./useResolvedThemeMode";

/**
 * Whether terminal surfaces render dark. The terminal theme setting is
 * independent of the app's: "auto" follows the app's resolved appearance,
 * while "light"/"dark" pin it. A Settings change re-renders live via the
 * preference's pub/sub.
 */
export function useTerminalIsDark(): boolean {
  const appIsDark = useResolvedThemeMode() === "dark";
  const [mode, setMode] = useState<TerminalThemeMode>(readTerminalThemeMode);
  useEffect(() => subscribeTerminalTheme(setMode), []);
  return resolveTerminalIsDark(mode, appIsDark);
}
