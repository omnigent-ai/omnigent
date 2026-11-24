// ⌘K rows for settings people change often: the appearance mode, the terminal theme and two
// chat switches, each one row with a page of its values. Each shares its name, choices and
// write path with its Settings control.

import { useMemo } from "react";
import { useTheme } from "next-themes";
import { FoldVerticalIcon, type LucideIcon, MonitorIcon, SendIcon } from "lucide-react";

import { normalizeThemeMode, type ThemeMode } from "@/components/theme/themeMode";
import {
  TERMINAL_THEME_OPTIONS,
  THEME_MODE_OPTIONS,
  themeModeLabel,
} from "@/components/theme/themeModeOptions";
import { useTerminalThemeMode } from "@/components/theme/useTerminalIsDark";
import { useAlwaysSteer, writeAlwaysSteer } from "@/lib/alwaysSteerPreferences";
import { useHostTheme } from "@/lib/embedded";
import {
  useFoldAssistantNarration,
  writeFoldAssistantNarration,
} from "@/lib/foldNarrationPreferences";
import {
  isTerminalThemeMode,
  type TerminalThemeMode,
  writeTerminalThemeMode,
} from "@/lib/terminalThemePreferences";
import { ALWAYS_STEER_ENTRY } from "@/pages/settings/search/catalog/general";
import {
  SHOW_FINAL_ANSWER_ENTRY,
  TERMINAL_THEME_ENTRY,
} from "@/pages/settings/search/catalog/appearance";
import type { PaletteCommand } from "@/store/paletteCommands";
import { booleanSetting, type PaletteSetting, settingCommand } from "@/store/paletteSettings";

/** An on/off setting, named as Settings shows it. */
interface SettingSwitch {
  id: string;
  label: string;
  icon: LucideIcon;
  keywords: readonly string[];
}

export const SHOW_FINAL_ANSWER_SETTING: SettingSwitch = {
  id: SHOW_FINAL_ANSWER_ENTRY.id,
  label: SHOW_FINAL_ANSWER_ENTRY.label,
  icon: FoldVerticalIcon,
  keywords: SHOW_FINAL_ANSWER_ENTRY.keywords,
};

export const ALWAYS_STEER_SETTING: SettingSwitch = {
  id: ALWAYS_STEER_ENTRY.id,
  label: ALWAYS_STEER_ENTRY.label,
  icon: SendIcon,
  keywords: ALWAYS_STEER_ENTRY.keywords,
};

/** The appearance mode; embedded in Databricks, System reads "Use Databricks setting". */
export function appearanceModeSetting(
  current: ThemeMode,
  followsHost: boolean,
  setMode: (mode: ThemeMode) => void,
): PaletteSetting {
  return {
    id: "appearance-mode",
    owner: "app",
    label: "Appearance",
    icon: THEME_MODE_OPTIONS.find((option) => option.mode === current)?.icon ?? MonitorIcon,
    keywords: ["theme", "mode", "dark mode", "light mode"],
    current,
    choices: THEME_MODE_OPTIONS.map((option) => ({
      value: option.mode,
      label: themeModeLabel(option.mode, followsHost),
    })),
    choose: (mode) => setMode(normalizeThemeMode(mode)),
  };
}

/** The terminal theme: Match app, Light or Dark. */
export function terminalThemeSetting(
  current: TerminalThemeMode,
  write: (mode: TerminalThemeMode) => void = writeTerminalThemeMode,
): PaletteSetting {
  return {
    id: TERMINAL_THEME_ENTRY.id,
    owner: "app",
    label: TERMINAL_THEME_ENTRY.label,
    icon: TERMINAL_THEME_OPTIONS.find((option) => option.mode === current)?.icon ?? MonitorIcon,
    keywords: TERMINAL_THEME_ENTRY.keywords,
    current,
    choices: TERMINAL_THEME_OPTIONS.map((option) => ({
      value: option.mode,
      label: option.label,
    })),
    choose: (mode) => {
      if (isTerminalThemeMode(mode)) write(mode);
    },
  };
}

/** An on/off switch, named as Settings shows it. */
export function settingSwitchSetting(
  setting: SettingSwitch,
  enabled: boolean,
  write: (enabled: boolean) => void,
): PaletteSetting {
  return booleanSetting({ ...setting, owner: "app", enabled, write });
}

/** Every setting row above, current as the settings change here or in another window. */
export function useSettingsCommands(): PaletteCommand[] {
  const { theme, setTheme } = useTheme();
  const mode = normalizeThemeMode(theme);
  const followsHost = useHostTheme() !== undefined;
  const terminalTheme = useTerminalThemeMode();
  const showFinalAnswer = useFoldAssistantNarration();
  const alwaysSteer = useAlwaysSteer();
  return useMemo(
    () => [
      settingCommand(appearanceModeSetting(mode, followsHost, setTheme)),
      settingCommand(terminalThemeSetting(terminalTheme)),
      settingCommand(
        settingSwitchSetting(
          SHOW_FINAL_ANSWER_SETTING,
          showFinalAnswer,
          writeFoldAssistantNarration,
        ),
      ),
      settingCommand(settingSwitchSetting(ALWAYS_STEER_SETTING, alwaysSteer, writeAlwaysSteer)),
    ],
    [mode, followsHost, setTheme, terminalTheme, showFinalAnswer, alwaysSteer],
  );
}
