// What the Keyboard shortcuts page lists for a query typed into its search field.

import type { KeyChordInput } from "@/lib/shortcuts/chord";
import type { ShortcutEnvironment, ShortcutId } from "@/lib/shortcuts/registry";
import type { ShortcutPageGroup, ShortcutPageRow } from "@/lib/shortcuts/shortcutPage";

import { parseChordQuery, rowsForChord } from "./search/chordQuery";

const words = (text: string) => text.toLocaleLowerCase().split(/\s+/).filter(Boolean);

/** The text a row is found by: its name, its group, and its keys as the page draws them. */
function rowText(row: ShortcutPageRow, group: ShortcutPageGroup): string {
  return [row.label, group.title, group.note ?? "", ...row.keys, ...(row.alternateKeys ?? [])]
    .join(" ")
    .toLocaleLowerCase();
}

/** Whether two chords press the same keys, ignoring the letter's case. */
function sameChord(left: KeyChordInput, right: KeyChordInput): boolean {
  return (
    left.key.toLocaleLowerCase() === right.key.toLocaleLowerCase() &&
    left.metaKey === right.metaKey &&
    left.ctrlKey === right.ctrlKey &&
    left.altKey === right.altKey &&
    left.shiftKey === right.shiftKey
  );
}

/** Whether the row shows `chord` as its keys or their alternative, as the page draws them. */
function displaysChord(row: ShortcutPageRow, chord: KeyChordInput, mac: boolean): boolean {
  return [row.keys, row.alternateKeys ?? []].some((keys) => {
    const shown = keys.length > 0 ? parseChordQuery(keys.join(" "), mac) : null;
    return shown !== null && sameChord(shown, chord);
  });
}

/**
 * The groups and rows that match `query`: a row whose name, group or keys contain every word of
 * it, or one the typed chord presses or shows (such as "⌘K" or "cmd+k"). Empty groups are left out.
 */
export function filterShortcutPage(
  groups: readonly ShortcutPageGroup[],
  query: string,
  environment: ShortcutEnvironment,
): ShortcutPageGroup[] {
  const parts = words(query);
  if (parts.length === 0) return [...groups];
  const chord = parseChordQuery(query, environment.mac);
  const listed = new Set<ShortcutId>(
    groups.flatMap((group) => group.items.flatMap((row) => row.shortcutId ?? [])),
  );
  const pressed = chord ? rowsForChord(chord, environment, listed) : new Set<ShortcutId>();
  return groups.flatMap((group) => {
    const items = group.items.filter((row) => {
      if (row.shortcutId !== undefined && pressed.has(row.shortcutId)) return true;
      // A chord typed as the keys a row draws, such as the browser's ⌘⇧O for New session.
      if (chord && displaysChord(row, chord, environment.mac)) return true;
      const text = rowText(row, group);
      return parts.every((part) => text.includes(part));
    });
    return items.length > 0 ? [{ ...group, items }] : [];
  });
}
