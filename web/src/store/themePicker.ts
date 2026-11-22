import { create } from "zustand";

import type { PaletteCommand } from "./paletteCommands";

// The "Select theme" picker that ⌘K opens, and the command that opens it.

export const SELECT_THEME_COMMAND: Pick<PaletteCommand, "id" | "label" | "keywords"> = {
  id: "select-theme",
  label: "Select theme…",
  keywords: ["color", "colour", "palette", "appearance", "dark mode", "light mode", "scheme"],
};

interface ThemePickerState {
  open: boolean;
  /**
   * Another dialog took the picker's place. It goes at once rather than
   * animating out, so it never sits behind the new dialog taking its keys.
   */
  replaced: boolean;
}

export const useThemePickerStore = create<ThemePickerState>(() => ({
  open: false,
  replaced: false,
}));

export function openThemePicker(): void {
  useThemePickerStore.setState({ open: true, replaced: false });
}

export function closeThemePicker(): void {
  useThemePickerStore.setState({ open: false });
}

/** Close the picker because another dialog, such as the command palette, opened in its place. */
export function replaceThemePicker(): void {
  if (!useThemePickerStore.getState().open) return;
  useThemePickerStore.setState({ open: false, replaced: true });
}
