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

export const APPEARANCE_SETTINGS = [
  THEME_MODE_ENTRY,
  TERMINAL_THEME_ENTRY,
  COLOR_THEME_ENTRY,
] as const;
