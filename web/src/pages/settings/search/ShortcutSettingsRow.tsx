import { type ReactNode, useRef } from "react";

import type { ShortcutPageGroup, ShortcutPageRow } from "@/lib/shortcuts/shortcutPage";
import { cn } from "@/lib/utils";

import { useSettingsRowReveal } from "./SettingsItem";
import { SETTINGS_HIGHLIGHT_CLASS } from "./settingsReveal";
import { shortcutSettingsEntry } from "./shortcutEntries";

interface ShortcutSettingsRowProps {
  row: ShortcutPageRow;
  group: ShortcutPageGroup;
  className?: string;
  children: ReactNode;
}

/** A Keyboard shortcuts row in Settings, which search can reveal and highlight. */
export function ShortcutSettingsRow({ row, group, className, children }: ShortcutSettingsRowProps) {
  const entry = shortcutSettingsEntry(row, group);
  const rowRef = useRef<HTMLLIElement | null>(null);
  useSettingsRowReveal(entry, rowRef);
  return (
    <li
      ref={rowRef}
      data-settings-item={entry.id}
      className={cn(SETTINGS_HIGHLIGHT_CLASS, className)}
    >
      {children}
    </li>
  );
}
