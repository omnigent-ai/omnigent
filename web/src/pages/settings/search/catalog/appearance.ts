import { PALETTES } from "@/lib/themePalette";

import { settingsEntry } from "./types";

export const THEME_MODE_ENTRY = settingsEntry({
  id: "theme-mode",
  section: "appearance",
  label: "Mode",
  description: "Follow your system, or force light or dark.",
  keywords: ["dark mode", "light mode", "theme", "system", "appearance", "databricks"],
  // As the radios name them; "Use Databricks setting" replaces System under a Databricks host.
  choices: ["System", "Use Databricks setting", "Light", "Dark"],
  parts: { databricksSettings: "Databricks appearance settings" },
  presence: { databricksSettings: { states: ["follows Databricks"] } },
});

export const TERMINAL_THEME_ENTRY = settingsEntry({
  id: "terminal-theme",
  section: "appearance",
  label: "Terminal theme",
  description: "Use a light or dark terminal, or match the app.",
  keywords: ["terminal", "dark", "light", "colors"],
  // As the terminal theme radios name them.
  choices: ["Match app", "Light", "Dark"],
});

export const COLOR_THEME_ENTRY = settingsEntry({
  id: "color-theme",
  section: "appearance",
  label: "Color theme",
  description: "Choose a preset, then tune it across light and dark mode.",
  keywords: ["theme", "palette", "colors", "accent", "tint", "contrast", "translucent"],
  // The palette select's choices, as it lists them.
  choices: [...PALETTES.map((palette) => palette.label), "Custom"],
  parts: {
    accent: "Accent",
    tint: "Background tint",
    contrast: "Theme contrast",
    translucentSidebars: "Translucent sidebars",
  },
  // A color's swatch is named with its hex value, such as "Accent: #3366FF".
  nameTemplates: {
    accent: { before: "Accent: " },
    tint: { before: "Background tint: " },
  },
  // The sub-labels the palette select and the contrast slider show beside them.
  partLabels: { "": "Theme palette", contrast: "Contrast" },
});

export const UI_FONT_SIZE_ENTRY = settingsEntry({
  id: "ui-font-size",
  section: "appearance",
  label: "Interface font size",
  description: "Sets interface and conversation text. Icons and spacing stay fixed.",
  keywords: ["font", "text size", "zoom", "type", "typography"],
  parts: {
    field: "Interface font size in pixels",
    decrease: "Decrease interface font size",
    increase: "Increase interface font size",
  },
  focus: "field",
});

export const UI_FONT_FAMILY_ENTRY = settingsEntry({
  id: "ui-font-family",
  section: "appearance",
  label: "Font family",
  description: "Use an installed font. Leave blank for the system default.",
  keywords: ["font", "typeface", "interface font", "typography"],
  parts: { field: "UI font family", reset: "Reset" },
  focus: "field",
});

export const CODE_FONT_SIZE_ENTRY = settingsEntry({
  id: "code-font-size",
  section: "appearance",
  label: "Code font size",
  description: "Size of code in the editor and terminal.",
  keywords: ["font", "editor", "terminal", "monospace", "typography"],
  parts: {
    field: "Code font size in pixels",
    decrease: "Decrease code font size",
    increase: "Increase code font size",
  },
  focus: "field",
});

export const CODE_FONT_FAMILY_ENTRY = settingsEntry({
  id: "code-font-family",
  section: "appearance",
  label: "Code font family",
  description: "Font for the editor and terminal. Leave blank for the default.",
  keywords: ["font", "monospace", "editor", "terminal", "typography"],
  parts: { field: "Code font family", reset: "Reset" },
  focus: "field",
});

export const CODE_FONT_WEIGHT_ENTRY = settingsEntry({
  id: "code-font-weight",
  section: "appearance",
  label: "Heavier code font",
  description: "Use heavier text in the editor and terminal.",
  keywords: ["bold", "weight", "font", "editor", "terminal"],
  parts: { toggle: "Use heavier code text" },
  focus: "toggle",
});

export const APPEARANCE_SETTINGS = [
  THEME_MODE_ENTRY,
  TERMINAL_THEME_ENTRY,
  COLOR_THEME_ENTRY,
  UI_FONT_SIZE_ENTRY,
  UI_FONT_FAMILY_ENTRY,
  CODE_FONT_SIZE_ENTRY,
  CODE_FONT_FAMILY_ENTRY,
  CODE_FONT_WEIGHT_ENTRY,
] as const;
