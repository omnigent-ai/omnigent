import { create } from "zustand";

import {
  applyThemePalette,
  readThemePalette,
  type ThemeSelection,
  writeThemePalette,
} from "@/lib/themePalette";

// The committed color palette, shared by Settings and the theme picker so a
// choice made in one shows in the other. Previews never change it.

interface ThemePaletteState {
  selection: ThemeSelection;
}

export const useThemePaletteStore = create<ThemePaletteState>(() => ({
  selection: readThemePalette(),
}));

export function useThemePaletteSelection(): ThemeSelection {
  return useThemePaletteStore((state) => state.selection);
}

/** Persists and applies a palette choice. */
export function commitThemePalette(selection: ThemeSelection): void {
  writeThemePalette(selection);
  applyThemePalette(selection);
  useThemePaletteStore.setState({ selection });
}

/** Re-reads the stored palette after something else wrote it, such as a settings import. */
export function reloadThemePalette(): void {
  const selection = readThemePalette();
  applyThemePalette(selection);
  useThemePaletteStore.setState({ selection });
}
