// A dragged strip entry's drop, as the tab order it leaves. The strip shows a split once,
// at its first member's place, while its member tabs may sit anywhere in the tab order, so
// a drop moves entries, never indexes into the tabs.

/** One strip entry and the tabs it stands for, in the strip's order. */
export interface StripReorderEntry<Id> {
  key: string;
  tabIds: readonly Id[];
}

/**
 * The full tab order after moving the entry `activeKey` to `overKey`'s place, as dnd-kit's
 * sortable list does: past it when dragged forward, before it when dragged back. Each entry's
 * tabs come out together, in their order. Null when nothing moves or a key isn't in the strip.
 */
export function reorderStripEntries<Id>(
  entries: readonly StripReorderEntry<Id>[],
  activeKey: string,
  overKey: string,
): Id[] | null {
  if (activeKey === overKey) return null;
  const from = entries.findIndex((entry) => entry.key === activeKey);
  const to = entries.findIndex((entry) => entry.key === overKey);
  if (from === -1 || to === -1) return null;
  const moved = entries.filter((_, index) => index !== from);
  moved.splice(to, 0, entries[from]);
  return moved.flatMap((entry) => entry.tabIds);
}
