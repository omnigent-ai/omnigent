import { create } from "zustand";

import type { PaletteCommand } from "./paletteCommands";

export const FEATURE_FLAGS_COMMAND: Pick<PaletteCommand, "id" | "label" | "keywords"> = {
  id: "override-ui-feature-flag",
  label: "Override UI feature flag…",
  keywords: ["feature flag", "flags", "override", "experiment", "preview", "enable", "disable"],
};

interface FeatureFlagsPickerState {
  open: boolean;
  replaced: boolean;
}

export const useFeatureFlagsPickerStore = create<FeatureFlagsPickerState>(() => ({
  open: false,
  replaced: false,
}));

export function openFeatureFlagsPicker(): void {
  useFeatureFlagsPickerStore.setState({ open: true, replaced: false });
}

export function closeFeatureFlagsPicker(): void {
  useFeatureFlagsPickerStore.setState({ open: false });
}

export function replaceFeatureFlagsPicker(): void {
  if (!useFeatureFlagsPickerStore.getState().open) return;
  useFeatureFlagsPickerStore.setState({ open: false, replaced: true });
}
