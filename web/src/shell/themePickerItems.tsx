// Rows of the "Select theme" picker: the current theme, the Appearance page,
// Omnigent Default, then the other palettes and Custom.

import { ContrastIcon } from "lucide-react";

import { PaletteChip } from "@/components/theme/AppearancePreviews";
import type { ThemeMode } from "@/components/theme/themeMode";
import { THEME_MODE_OPTIONS, themeModeLabel } from "@/components/theme/themeModeOptions";
import type { QuickPickItem } from "@/components/ui/quick-pick";
import {
  DEFAULT_PALETTE,
  PALETTES,
  type PaletteSwatch,
  type ThemeSelection,
} from "@/lib/themePalette";

export type ThemePickerPageId = "themes" | "appearance";

export type ThemePickerValue =
  | { kind: "palette"; selection: ThemeSelection }
  | { kind: "appearance" }
  | { kind: "mode"; mode: ThemeMode };

export const CURRENT_THEME_ITEM_ID = "theme:current";
export const APPEARANCE_ITEM_ID = "theme:appearance";
export const DEFAULT_THEME_ITEM_ID = "theme:default";

export const THEMES_GROUP = "Themes";

export function paletteItemId(selection: ThemeSelection): string {
  return `theme:palette:${selection}`;
}

export function modeItemId(mode: ThemeMode): string {
  return `appearance:${mode}`;
}

const DEFAULT_THEME_LABEL = "Omnigent Default";
const CUSTOM_LABEL = "Custom";

export interface ThemePickerContext {
  /** The committed palette. */
  selection: ThemeSelection;
  mode: ThemeMode;
  /** Chips render in the mode the app shows now. */
  isDark: boolean;
  /** Embedded in Databricks, System follows the workspace. */
  followsHost: boolean;
  customSwatches: { light: PaletteSwatch; dark: PaletteSwatch };
}

function swatchesFor(
  selection: ThemeSelection,
  context: ThemePickerContext,
): { light: PaletteSwatch; dark: PaletteSwatch } {
  if (selection === "custom") return context.customSwatches;
  return PALETTES.find((palette) => palette.id === selection) ?? PALETTES[0]!;
}

export function themeSelectionLabel(selection: ThemeSelection): string {
  if (selection === "custom") return CUSTOM_LABEL;
  if (selection === DEFAULT_PALETTE) return DEFAULT_THEME_LABEL;
  return PALETTES.find((palette) => palette.id === selection)?.label ?? DEFAULT_THEME_LABEL;
}

function chip(selection: ThemeSelection, context: ThemePickerContext) {
  const swatches = swatchesFor(selection, context);
  return <PaletteChip swatch={context.isDark ? swatches.dark : swatches.light} />;
}

function paletteItem(
  selection: ThemeSelection,
  context: ThemePickerContext,
  group: string,
): QuickPickItem<ThemePickerValue> {
  const palette = PALETTES.find((candidate) => candidate.id === selection);
  return {
    id: paletteItemId(selection),
    label: themeSelectionLabel(selection),
    keywords: palette ? [palette.blurb] : ["your colors", "edit in settings"],
    group,
    icon: chip(selection, context),
    detail: selection === context.selection ? "Current" : undefined,
    rememberInHistory: false,
    value: { kind: "palette", selection },
  };
}

/** The root page. */
export function themePageItems(context: ThemePickerContext): QuickPickItem<ThemePickerValue>[] {
  const modeLabel = themeModeLabel(context.mode, context.followsHost);
  const isDefault = context.selection === DEFAULT_PALETTE;
  const items: QuickPickItem<ThemePickerValue>[] = [
    {
      id: CURRENT_THEME_ITEM_ID,
      label: isDefault ? DEFAULT_THEME_LABEL : "Current theme",
      detail: isDefault ? modeLabel : `${themeSelectionLabel(context.selection)} · ${modeLabel}`,
      keywords: ["current", "keep"],
      icon: chip(context.selection, context),
      rememberInHistory: false,
      value: { kind: "palette", selection: context.selection },
    },
    {
      id: APPEARANCE_ITEM_ID,
      label: "Appearance",
      detail: modeLabel,
      keywords: ["mode", "light", "dark", "system"],
      icon: <ContrastIcon />,
      opensPage: true,
      rememberInHistory: false,
      value: { kind: "appearance" },
    },
  ];
  if (!isDefault) {
    items.push({
      id: DEFAULT_THEME_ITEM_ID,
      label: DEFAULT_THEME_LABEL,
      keywords: ["default", "reset", "omnigent", "pink"],
      icon: chip(DEFAULT_PALETTE, context),
      rememberInHistory: false,
      value: { kind: "palette", selection: DEFAULT_PALETTE },
    });
  }
  for (const palette of PALETTES) {
    if (palette.id === DEFAULT_PALETTE) continue;
    items.push(paletteItem(palette.id, context, THEMES_GROUP));
  }
  items.push(paletteItem("custom", context, THEMES_GROUP));
  return items;
}

const MODE_DESCRIPTIONS: Record<ThemeMode, { standalone: string; embedded: string }> = {
  system: { standalone: "Follow your system", embedded: "Follow Databricks" },
  light: { standalone: "Always light", embedded: "Always light" },
  dark: { standalone: "Always dark", embedded: "Always dark" },
};

/** The Appearance page: System, Light, Dark. */
export function appearancePageItems(
  context: Pick<ThemePickerContext, "mode" | "followsHost">,
): QuickPickItem<ThemePickerValue>[] {
  return THEME_MODE_OPTIONS.map(({ mode, icon: Icon }) => ({
    id: modeItemId(mode),
    label: themeModeLabel(mode, context.followsHost),
    description: MODE_DESCRIPTIONS[mode][context.followsHost ? "embedded" : "standalone"],
    detail: mode === context.mode ? "Current" : undefined,
    icon: <Icon />,
    rememberInHistory: false,
    value: { kind: "mode", mode },
  }));
}
