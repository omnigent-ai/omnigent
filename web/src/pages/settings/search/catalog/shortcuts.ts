// The Keyboard shortcuts page's own controls; its rows come from the shortcut registry.

import { settingsEntry } from "./types";

export const SHORTCUTS_SEARCH_ENTRY = settingsEntry({
  id: "shortcuts-search",
  section: "shortcuts",
  label: "Search shortcuts",
  description: "Filter the keyboard shortcuts by name, group or keys.",
  keywords: ["filter", "find", "keyboard", "hotkey", "chord"],
  parts: { search: "Search shortcuts" },
  available: (availability) => availability.quickPickPalette,
});

export const SHORTCUTS_SETTINGS = [SHORTCUTS_SEARCH_ENTRY] as const;
