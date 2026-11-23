// The Keyboard shortcuts page: its groups and rows, in order, for the current runtime. Every
// row is a registry binding, listed in registry order under its group with the chord shown
// here: the page's own, or the desktop menu's where the menu owns it.

import { isMacPlatform } from "@/lib/hotkeys";
import { chordKeys } from "@/lib/shortcuts/chord";
import {
  APP_SHORTCUT_BINDINGS,
  currentShortcutEnvironment,
  menuShortcutChords,
  primaryShortcutChords,
  SHORTCUT_GROUPS,
  type ShortcutBinding,
  type ShortcutEnvironment,
  type ShortcutGroupId,
  type ShortcutId,
  type ShortcutPageContext,
  shortcutDisplayRow,
} from "@/lib/shortcuts/registry";

export type { ShortcutPageContext } from "@/lib/shortcuts/registry";

export interface ShortcutPageRow {
  label: string;
  /** Keys drawn left to right: a chord, or two interchangeable keys such as ↑ ↓. */
  keys: string[];
  /** Another chord for the same action, shown after "or". */
  alternateKeys?: string[];
  /** Hidden on touch and narrow layouts, where Enter inserts a newline instead of submitting. */
  desktopOnly?: boolean;
  /** The registry binding behind the row, which gives its label and keys. */
  shortcutId: ShortcutId;
}

export interface ShortcutPageGroup {
  title: string;
  /** A qualifier shown next to the title. */
  note?: string;
  items: ShortcutPageRow[];
}

/** Key caps for the chord `id` shows here: its own first chord, else the desktop menu's. */
function shownKeys(id: ShortcutId, environment: ShortcutEnvironment): string[] {
  const chord = primaryShortcutChords(id, environment)[0] ?? menuShortcutChords(id, environment)[0];
  return chord ? chordKeys(chord, environment.mac) : [];
}

function pageRow(
  binding: ShortcutBinding,
  context: ShortcutPageContext,
  environment: ShortcutEnvironment,
): ShortcutPageRow | null {
  const keys = shownKeys(binding.id, environment);
  const rowKeys = binding.rowKeys ? binding.rowKeys(keys, { context, environment }) : keys;
  if (rowKeys.length === 0) return null;
  const alternate = APP_SHORTCUT_BINDINGS.find((candidate) => candidate.alternateOf === binding.id);
  const alternateShown = alternate && (alternate.advertised?.(environment) ?? true);
  return {
    label: binding.label,
    keys: rowKeys,
    // A row with an alternative lists it after "or" only where its chord applies.
    ...(alternate
      ? { alternateKeys: alternateShown ? shownKeys(alternate.id, environment) : undefined }
      : {}),
    ...(binding.desktopOnly ? { desktopOnly: true } : {}),
    shortcutId: binding.id,
  };
}

/** The page's groups for this runtime, leaving out groups with no rows. */
export function shortcutPageGroups(
  context: ShortcutPageContext,
  mac: boolean = isMacPlatform(),
): ShortcutPageGroup[] {
  const environment = currentShortcutEnvironment({
    mac,
    native: context.native,
    desktopMenu: context.desktopCommands,
    submitWithModEnter: context.submitWithModEnter,
  });
  const rows = new Map<ShortcutGroupId, ShortcutPageRow[]>();
  for (const binding of APP_SHORTCUT_BINDINGS) {
    // An alternate or a part is drawn on the row it belongs to.
    if (shortcutDisplayRow(binding.id) !== binding.id) continue;
    if (!(binding.shownWhen?.(context) ?? true)) continue;
    if (binding.desktopOnly && context.preventsKeyboardSubmit) continue;
    const row = pageRow(binding, context, environment);
    if (row) rows.set(binding.group, [...(rows.get(binding.group) ?? []), row]);
  }
  return SHORTCUT_GROUPS.flatMap(({ id, title, note }) => {
    const items = rows.get(id) ?? [];
    if (items.length === 0) return [];
    return [note === undefined ? { title, items } : { title, note, items }];
  });
}
