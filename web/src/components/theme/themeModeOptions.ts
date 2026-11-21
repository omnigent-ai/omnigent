// The appearance and terminal theme choices, named and drawn the same way wherever they're
// offered: the Settings controls and ⌘K's setting rows.

import { type LucideIcon, MonitorIcon, MoonIcon, SunIcon } from "lucide-react";

import type { TerminalThemeMode } from "@/lib/terminalThemePreferences";

import type { ThemeMode } from "./themeMode";

export interface ThemeModeOption<Mode extends string> {
  mode: Mode;
  label: string;
  icon: LucideIcon;
}

export const THEME_MODE_OPTIONS: readonly ThemeModeOption<ThemeMode>[] = [
  { mode: "system", label: "System", icon: MonitorIcon },
  { mode: "light", label: "Light", icon: SunIcon },
  { mode: "dark", label: "Dark", icon: MoonIcon },
];

/** A mode's name; embedded in Databricks, System follows the host's setting. */
export function themeModeLabel(mode: ThemeMode, followsHost: boolean): string {
  if (mode === "system" && followsHost) return "Use Databricks setting";
  return THEME_MODE_OPTIONS.find((option) => option.mode === mode)?.label ?? mode;
}

export const TERMINAL_THEME_OPTIONS: readonly ThemeModeOption<TerminalThemeMode>[] = [
  { mode: "auto", label: "Match app", icon: MonitorIcon },
  { mode: "light", label: "Light", icon: SunIcon },
  { mode: "dark", label: "Dark", icon: MoonIcon },
];
