import { type ReactNode, useRef } from "react";

import type { ShortcutPageGroup, ShortcutPageRow } from "@/lib/shortcuts/shortcutPage";
import { cn } from "@/lib/utils";

import { useSettingsRowReveal } from "./SettingsItem";
import { SETTINGS_HIGHLIGHT_CLASS } from "./settingsReveal";
import {
  type BoundShortcutRow,
  isBoundShortcutRow,
  shortcutSettingsEntry,
} from "./shortcutEntries";

interface ShortcutSettingsRowProps {
  row: ShortcutPageRow;
  group: ShortcutPageGroup;
  className?: string;
  children: ReactNode;
}

/**
 * A Keyboard shortcuts row in Settings. A row the registry binds can be revealed by search; an
 * upstream-only row is read-only and has nothing to reveal.
 */
export function ShortcutSettingsRow({ row, group, className, children }: ShortcutSettingsRowProps) {
  if (!isBoundShortcutRow(row)) return <li className={className}>{children}</li>;
  return (
    <RevealableShortcutRow row={row} group={group} className={className}>
      {children}
    </RevealableShortcutRow>
  );
}

function RevealableShortcutRow({
  row,
  group,
  className,
  children,
}: ShortcutSettingsRowProps & { row: BoundShortcutRow }) {
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
