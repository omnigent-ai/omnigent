// The "Select theme" picker, opened from ⌘K. The
// highlighted palette, or appearance on its page, previews across the app;
// Escape, Back or a click outside restores the committed one, and Enter keeps it.

import { useTheme } from "next-themes";
import { useEffect, useMemo } from "react";

import { normalizeThemeMode, type ThemeMode } from "@/components/theme/themeMode";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import { type QuickPickItem, QuickPickDialog, useQuickPickPages } from "@/components/ui/quick-pick";
import { useLivePreview } from "@/hooks/useLivePreview";
import { customThemeSwatches, readCustomTheme } from "@/lib/customTheme";
import { useHostTheme } from "@/lib/embedded";
import { applyThemePalette, type ThemeSelection } from "@/lib/themePalette";
import { setThemeModePreview } from "@/store/themeModePreview";
import { commitThemePalette, useThemePaletteStore } from "@/store/themePalette";
import { closeThemePicker, useThemePickerStore } from "@/store/themePicker";

import {
  appearancePageItems,
  CURRENT_THEME_ITEM_ID,
  modeItemId,
  type ThemePickerPageId,
  type ThemePickerValue,
  themePageItems,
} from "./themePickerItems";

export function ThemePicker() {
  const open = useThemePickerStore((state) => state.open);
  const replaced = useThemePickerStore((state) => state.replaced);
  const selection = useThemePaletteStore((state) => state.selection);
  const { theme, setTheme } = useTheme();
  const mode = normalizeThemeMode(theme);
  const isDark = useResolvedThemeMode() === "dark";
  const followsHost = useHostTheme() !== undefined;
  const pages = useQuickPickPages<ThemePickerPageId>("themes", CURRENT_THEME_ITEM_ID);
  const { reset } = pages;
  // However it closed, the next opening starts on the themes page.
  useEffect(() => {
    if (!open) reset();
  }, [open, reset]);

  // One palette preview per opening, and one appearance preview per visit to
  // its page, so Back reverts the appearance and leaving any way reverts both.
  const palettePreview = useLivePreview<ThemeSelection>({
    active: open,
    readCommitted: () => useThemePaletteStore.getState().selection,
    apply: applyThemePalette,
    commit: commitThemePalette,
  });
  const onAppearancePage = pages.page.id === "appearance";
  // The appearance baseline is "no override", so reverting always shows the
  // latest saved mode, even one another window saved meanwhile.
  const modePreview = useLivePreview<ThemeMode | null>({
    active: open && onAppearancePage,
    readCommitted: () => null,
    apply: setThemeModePreview,
    commit: (chosen) => {
      if (chosen !== null) setTheme(chosen);
      setThemeModePreview(null);
    },
  });

  const items = useMemo(() => {
    if (!open) return [];
    if (onAppearancePage) return appearancePageItems({ mode, followsHost });
    return themePageItems({
      selection,
      mode,
      isDark,
      followsHost,
      customSwatches: customThemeSwatches(readCustomTheme()),
    });
  }, [open, onAppearancePage, selection, mode, isDark, followsHost]);

  const valueById = useMemo(() => new Map(items.map((item) => [item.id, item.value])), [items]);
  // The active row previews, whether the keys, the pointer or a filter made it active.
  const previewRow = (id: string) => {
    const value = valueById.get(id);
    palettePreview.preview(value?.kind === "palette" ? value.selection : null);
    modePreview.preview(value?.kind === "mode" ? value.mode : null);
  };

  const cancelPreviews = () => {
    modePreview.cancel();
    palettePreview.cancel();
  };
  const handleAccept = (item: QuickPickItem<ThemePickerValue>) => {
    const value = item.value;
    if (value.kind === "appearance") {
      pages.push("appearance", modeItemId(mode));
      return "keepOpen" as const;
    }
    if (value.kind === "mode") {
      modePreview.commit(value.mode);
      pages.pop();
      return "keepOpen" as const;
    }
    palettePreview.commit(value.selection);
    return undefined;
  };

  // Replaced by another dialog: unmount now, so the closing picker can't take its Escape.
  if (replaced) return null;
  return (
    <QuickPickDialog<ThemePickerValue>
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen) closeThemePicker();
      }}
      onCancel={cancelPreviews}
      // A clear backdrop shows the previewed palette across the whole app; the
      // picker frosts what's behind it, as menus do, so translucent surfaces stay readable.
      overlayClassName="bg-transparent supports-backdrop-filter:backdrop-blur-none"
      contentClassName="supports-backdrop-filter:backdrop-blur-xl"
      title="Select theme"
      description="Use the arrow keys to preview a theme or appearance, press Enter to keep it, or Escape to go back."
      label={onAppearancePage ? "Select appearance" : "Select theme"}
      placeholder={onAppearancePage ? "Select appearance…" : "Select theme…"}
      backLabel="Back to themes"
      segments={[{ sourceId: "themes", ranking: "client", items }]}
      {...pages.pickerProps}
      onActiveRowShown={previewRow}
      emptyMessage={onAppearancePage ? "No matching appearance" : "No matching themes"}
      acceptLabel={onAppearancePage ? "Apply" : "Select"}
      onAccept={handleAccept}
    />
  );
}
