// The splits in the sidebar's terminal tabs, as targets the shared pane commands
// act on. A shell already in a group splits that group; one on its own shows
// unsplit, and its first split makes a group. Layouts live in the session's
// terminal groups, and a group left with one pane goes, so that shell is an
// ordinary tab again.

import { groupOfTerminal, newTerminalGroupId, type TerminalGroup } from "@/lib/terminalGroups";
import { terminalLayoutKeys, type TerminalSplitState } from "@/lib/paneSplitLayout";

import { isClosingShell, readTerminalGroups, updateTerminalGroups } from "./terminalGroupsStore";
import type { TerminalSplitTarget } from "./terminalSplitTarget";

/** Write ``next`` for the group ``id`` (or a new one), dropping it once it would hold one pane. */
function saveGroupSplit(
  groups: readonly TerminalGroup[],
  id: string | null,
  next: TerminalSplitState | undefined,
): readonly TerminalGroup[] {
  const split = next && terminalLayoutKeys(next.layout).length > 1 ? next : undefined;
  if (id === null) return split ? [...groups, { id: newTerminalGroupId(), split }] : groups;
  if (!groups.some((group) => group.id === id)) return groups;
  return split
    ? groups.map((group) =>
        // Spread over the old split so fields a newer version saved there stay.
        group.id === id ? { ...group, split: { ...group.split, ...split } } : group,
      )
    : groups.filter((group) => group.id !== id);
}

/** Whether a close is ending any of ``group``'s shells, so it takes no changes. */
const isClosing = (conversationId: string, group: TerminalGroup) =>
  terminalLayoutKeys(group.split.layout).some((key) => isClosingShell(conversationId, key));

const targets = new Map<string, TerminalSplitTarget>();

function memoized(id: string, build: () => TerminalSplitTarget): TerminalSplitTarget {
  let target = targets.get(id);
  if (!target) {
    target = build();
    targets.set(id, target);
  }
  return target;
}

/** A sidebar group's split. Every pane in it is a shell, so any may close. */
export function sidebarGroupTarget(conversationId: string, groupId: string): TerminalSplitTarget {
  const id = `${conversationId}|sidebar|${groupId}`;
  return memoized(id, () => ({
    id,
    conversationId,
    read: () => readTerminalGroups(conversationId).find((group) => group.id === groupId)?.split,
    update: (update) =>
      updateTerminalGroups(conversationId, (groups) => {
        const current = groups.find((group) => group.id === groupId);
        if (!current || isClosing(conversationId, current)) return groups;
        const next = update(current.split);
        return next === current.split ? groups : saveGroupSplit(groups, groupId, next);
      }),
    // A group is always split; once it's gone, nothing is inserted into it.
    unsplitKey: () => null,
    canClosePane: () => true,
    focusSurface: "rail",
  }));
}

/** A sidebar shell's tab: its group's split once it has one, else the shell alone. */
export function sidebarShellTarget(conversationId: string, shellKey: string): TerminalSplitTarget {
  const id = `${conversationId}|sidebar-shell|${shellKey}`;
  return memoized(id, () => ({
    id,
    conversationId,
    read: () => groupOfTerminal(readTerminalGroups(conversationId), shellKey)?.split,
    update: (update) =>
      updateTerminalGroups(conversationId, (groups) => {
        const owner = groupOfTerminal(groups, shellKey);
        if (isClosingShell(conversationId, shellKey) || (owner && isClosing(conversationId, owner)))
          return groups;
        const next = update(owner?.split);
        if (next === owner?.split) return groups;
        return saveGroupSplit(groups, owner?.id ?? null, next);
      }),
    unsplitKey: () => shellKey,
    canClosePane: () => true,
    focusSurface: "rail",
  }));
}

/** The target for the sidebar tab showing ``shellKey``: its group's, or the shell's own. */
export function sidebarTargetFor(conversationId: string, shellKey: string): TerminalSplitTarget {
  const owner = groupOfTerminal(readTerminalGroups(conversationId), shellKey);
  return owner
    ? sidebarGroupTarget(conversationId, owner.id)
    : sidebarShellTarget(conversationId, shellKey);
}
