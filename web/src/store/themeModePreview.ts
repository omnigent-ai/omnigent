import { create } from "zustand";

import type { ThemeMode } from "@/components/theme/themeMode";

// The appearance a picker is previewing. It is never persisted: the saved mode
// stays in next-themes, and the provider shows this one on top while it is set.

interface ThemeModePreviewState {
  mode: ThemeMode | null;
}

export const useThemeModePreviewStore = create<ThemeModePreviewState>(() => ({ mode: null }));

export function useThemeModePreview(): ThemeMode | null {
  return useThemeModePreviewStore((state) => state.mode);
}

/** Shows `mode` without saving it, or the saved mode again for null. */
export function setThemeModePreview(mode: ThemeMode | null): void {
  useThemeModePreviewStore.setState({ mode });
}
