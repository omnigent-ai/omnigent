// Keyboard shortcut rows as search entries, generated from the rows the Keyboard shortcuts
// page lists, so the two can't disagree. An entry keeps the binding id, not key strings: the
// keys come from the live page row wherever an entry is shown.

import type { ShortcutId } from "@/lib/shortcuts/registry";
import type {
  ShortcutPageContext,
  ShortcutPageGroup,
  ShortcutPageRow,
} from "@/lib/shortcuts/shortcutPage";

import type { SettingsEntry } from "./catalog/types";

const SHORTCUT_KEYWORDS = ["shortcut", "keyboard", "hotkey", "keys"] as const;

// One object per binding and heading, so rows and effects that depend on an entry stay stable.
const entryCache = new Map<string, SettingsEntry>();

export function shortcutSettingsEntryId(row: Pick<ShortcutPageRow, "shortcutId">): string {
  return `shortcut.${row.shortcutId}`;
}

/** The entry for one page row, listed under `group`'s heading. */
export function shortcutSettingsEntry(
  row: ShortcutPageRow,
  group: ShortcutPageGroup,
): SettingsEntry {
  const id = shortcutSettingsEntryId(row);
  const cacheKey = `${id}\u0000${row.label}\u0000${group.title}`;
  const cached = entryCache.get(cacheKey);
  if (cached) return cached;
  const entry: SettingsEntry = {
    id,
    section: "shortcuts",
    label: row.label,
    description: group.title,
    keywords: SHORTCUT_KEYWORDS,
    shortcutId: row.shortcutId,
  };
  entryCache.set(cacheKey, entry);
  return entry;
}

// Without ⌘K's QuickPick, Settings search finds only the rows it found before every page row
// named its binding. Start a new session joins them only in the desktop app.
const ROWS_SEARCHED_WITHOUT_QUICK_PICK: ReadonlySet<ShortcutId> = new Set<ShortcutId>([
  "approval.accept",
  "approval.decline",
  "composer.focus",
  "dictation.toggle",
  "effort.cycle",
  "history.back",
  "history.forward",
  "model.cycle",
  "model.picker",
  "palette.commands",
  "palette.sessions",
  "pane.next",
  "pane.previous",
  "session.rename",
  "session.toggleUnread",
  "sessions.recent",
  "shortcuts.show",
  "sidebar.left",
  "sidebar.right",
  "terminal.new",
  "terminal.splitDown",
  "terminal.splitRight",
  "view.mode",
]);

/** What decides which page rows Settings search finds and reveals here. */
export type ShortcutSearchContext = Pick<ShortcutPageContext, "quickPick" | "native">;

/** Whether Settings search finds this page row, and the page lets search reveal it. */
export function isSearchedShortcutRow(
  row: Pick<ShortcutPageRow, "shortcutId">,
  context: ShortcutSearchContext,
): boolean {
  if (context.quickPick) return true;
  if (row.shortcutId === "session.new") return context.native;
  return ROWS_SEARCHED_WITHOUT_QUICK_PICK.has(row.shortcutId);
}

/** Every row the page lists for these groups that search finds here, in page order. */
export function shortcutSettingsEntries(
  groups: readonly ShortcutPageGroup[],
  context: ShortcutSearchContext,
): SettingsEntry[] {
  return groups.flatMap((group) =>
    group.items
      .filter((row) => isSearchedShortcutRow(row, context))
      .map((row) => shortcutSettingsEntry(row, group)),
  );
}
