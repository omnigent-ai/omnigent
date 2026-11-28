import type { Conversation } from "@/hooks/useConversations";
import { PINNED_LABEL_KEY } from "./sessionListCache";

/** Legacy personal pins do not detach members from their family. */
export function isSessionPinned(row: Conversation, pinnedIds?: ReadonlySet<string>): boolean {
  if (row.family?.pinned) return true;
  if (isIndentedSibling(row)) return pinnedIds?.has(row.family!.representative_id!) === true;
  return row.labels?.[PINNED_LABEL_KEY] !== undefined || pinnedIds?.has(row.id) === true;
}

/** Display groups retain a surviving representative while deletion metadata refreshes. */
export function sessionFamilyGroups(rows: readonly Conversation[]): Map<string, Conversation[]> {
  const families = new Map<string, Conversation[]>();
  for (const row of rows) {
    if (!row.family) continue;
    const members = families.get(row.family.id) ?? [];
    members.push(row);
    families.set(row.family.id, members);
  }
  // ponytail: visual promotion uses loaded peers; server metadata still owns membership.
  for (const [familyId, members] of families) {
    const representativeId = members.find((row) => row.family?.representative_id === row.id)?.id;
    members.sort(
      (a, b) =>
        Number(b.id === representativeId) - Number(a.id === representativeId) ||
        a.created_at - b.created_at ||
        Number(b.id === familyId) - Number(a.id === familyId) ||
        a.id.localeCompare(b.id),
    );
    // The head row stays first; nested sessions list newest first beneath it.
    members.push(...members.splice(1).reverse());
  }
  return families;
}

/** Retain each family's position, with its representative before any visible peers. */
export function orderSessionFamilies(
  rows: readonly Conversation[],
  collapsedFamilies?: ReadonlySet<string>,
): Conversation[] {
  const families = sessionFamilyGroups(rows);
  const emitted = new Set<string>();
  const result: Conversation[] = [];
  for (const row of rows) {
    if (emitted.has(row.id)) continue;
    const members = row.family ? families.get(row.family.id)! : [row];
    const collapsed = row.family && collapsedFamilies?.has(row.family.id);
    for (const member of members) {
      if (!emitted.has(member.id) && (!collapsed || member === members[0])) result.push(member);
      emitted.add(member.id);
    }
  }
  return result;
}

/**
 * The rows a collapsed project folder keeps listing for the open session: its family,
 * so nested sessions stay reachable. A collapsed family shows only its head, so an
 * open nested member of one is held alone.
 */
export function heldFolderRows(
  rows: readonly Conversation[],
  openId: string | null | undefined,
  collapsedFamilies: ReadonlySet<string>,
): Conversation[] {
  const open = rows.find((row) => row.id === openId);
  if (!open) return [];
  const familyId = open.family?.id;
  if (familyId === undefined) return [open];
  const members = sessionFamilyGroups(rows.filter((row) => row.family?.id === familyId)).get(
    familyId,
  )!;
  return collapsedFamilies.has(familyId) && members[0] !== open ? [open] : members;
}

export function isIndentedSibling(row: Conversation): boolean {
  return !!row.family?.representative_id && row.family.representative_id !== row.id;
}
