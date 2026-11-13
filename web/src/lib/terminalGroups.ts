// Sidebar terminal groups: several shells in one split, each keeping its own tab. The strip shows
// a group once, at its first member, and each shell is shown in one place at a time.

import { randomUUID } from "@/lib/randomUUID";
import {
  closeTerminalPane,
  parseTerminalSplitState,
  terminalLayoutKeys,
  type TerminalSplitState,
} from "./paneSplitLayout";

export interface TerminalGroup {
  /** Stable for the group's life: `g_` and a random id. */
  id: string;
  /** The panes, whose leaves are terminal keys, and the focused one. */
  split: TerminalSplitState;
}

const GROUP_ID = /^g_[A-Za-z0-9_-]{1,64}$/;

export function newTerminalGroupId(): string {
  return `g_${randomUUID().replaceAll("-", "")}`;
}

/** The groups in a saved value, each checked on its own; a bad entry is dropped. */
export function parseTerminalGroups(value: unknown): TerminalGroup[] {
  if (!Array.isArray(value)) return [];
  const groups: TerminalGroup[] = [];
  const seen = new Set<string>();
  for (const entry of value) {
    if (!entry || typeof entry !== "object") continue;
    const { id, split } = entry as Record<string, unknown>;
    if (typeof id !== "string" || !GROUP_ID.test(id) || seen.has(id)) continue;
    const parsed = parseTerminalSplitState(split);
    if (!parsed) continue;
    seen.add(id);
    // Fields a newer version added ride along, so writing this group back keeps them.
    groups.push({ ...entry, id, split: { ...(split as object), ...parsed } });
  }
  return groups;
}

export const groupKeys = (group: TerminalGroup): string[] => terminalLayoutKeys(group.split.layout);

/** The group showing ``terminalKey``, if any. */
export function groupOfTerminal(
  groups: readonly TerminalGroup[],
  terminalKey: string,
): TerminalGroup | undefined {
  return groups.find((group) => groupKeys(group).includes(terminalKey));
}

/** ``group`` without ``terminalKey``'s pane, or undefined once it would hold one pane. */
function dropPane(group: TerminalGroup, terminalKey: string): TerminalGroup | undefined {
  if (!groupKeys(group).includes(terminalKey)) return group;
  const split = closeTerminalPane(group.split, terminalKey);
  return split === undefined ? undefined : { ...group, split: { ...group.split, ...split } };
}

/**
 * ``groups`` with ``terminalKey`` out of its group. A group left with one pane
 * dissolves, so that shell is an ordinary tab again. Returns ``groups`` itself
 * when nothing changes.
 */
export function withoutTerminal(
  groups: readonly TerminalGroup[],
  terminalKey: string,
): readonly TerminalGroup[] {
  if (!groupOfTerminal(groups, terminalKey)) return groups;
  return groups.flatMap((group) => {
    const kept = dropPane(group, terminalKey);
    return kept ? [kept] : [];
  });
}

/**
 * Each shell in one place: the main view's split owns its shells first, then
 * groups in order. A shell already shown elsewhere leaves the later group's
 * layout, never ending the shell, and one ``isGone`` reports goes everywhere.
 * Only a group that survives places its shells, so one that dissolves frees them.
 * Returns ``groups`` itself when nothing changes.
 */
export function normalizeTerminalGroups(
  groups: readonly TerminalGroup[],
  { mainKeys, isGone }: { mainKeys: readonly string[]; isGone: (key: string) => boolean },
): readonly TerminalGroup[] {
  const placed = new Set(mainKeys);
  let changed = false;
  const kept: TerminalGroup[] = [];
  for (const original of groups) {
    let group: TerminalGroup | undefined = original;
    for (const key of groupKeys(original)) {
      if (group && (placed.has(key) || isGone(key))) group = dropPane(group, key);
    }
    if (group !== original) changed = true;
    if (!group) continue;
    for (const key of groupKeys(group)) placed.add(key);
    kept.push(group);
  }
  return changed ? kept : groups;
}

/** One strip entry: an ordinary tab, or a group shown once for all its members' tabs. */
export type StripEntry<Id> =
  { kind: "tab"; id: Id } | { kind: "group"; group: TerminalGroup; memberIds: Id[] };

/**
 * The strip for ``tabs`` in order, where ``terminalKeyOf`` names a tab's shell (or
 * null for any other tab). A group appears once, at its first member tab, and
 * collects every tab of its shells, duplicates included. A group with no tab
 * here isn't shown.
 */
export function projectStrip<Id>(
  tabs: readonly Id[],
  terminalKeyOf: (id: Id) => string | null,
  groups: readonly TerminalGroup[],
): StripEntry<Id>[] {
  const entries: StripEntry<Id>[] = [];
  const shown = new Map<string, Extract<StripEntry<Id>, { kind: "group" }>>();
  for (const id of tabs) {
    const key = terminalKeyOf(id);
    const group = key === null ? undefined : groupOfTerminal(groups, key);
    if (!group) {
      entries.push({ kind: "tab", id });
      continue;
    }
    const entry = shown.get(group.id);
    if (entry) {
      entry.memberIds.push(id);
    } else {
      const created = { kind: "group" as const, group, memberIds: [id] };
      shown.set(group.id, created);
      entries.push(created);
    }
  }
  return entries;
}
