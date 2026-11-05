import type { Conversation } from "@/hooks/useConversations";
import { PINNED_LABEL_KEY } from "./sessionListCache";

export function isIndividuallyPinnedSibling(row: Conversation): boolean {
  return isIndentedSibling(row) && row.labels?.[PINNED_LABEL_KEY] != null;
}

/** Display groups retain a surviving representative while deletion metadata refreshes. */
export function sessionFamilyGroups(rows: Conversation[]): Map<string, Conversation[]> {
  const families = new Map<string, Conversation[]>();
  for (const row of rows) {
    if (!row.family || isIndividuallyPinnedSibling(row)) continue;
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
  }
  return families;
}

/** Retain each family's sorted position, but put its representative before its peers. */
export function orderSessionFamilies(rows: Conversation[]): Conversation[] {
  const families = sessionFamilyGroups(rows);
  const emitted = new Set<string>();
  const result: Conversation[] = [];
  for (const row of rows) {
    if (emitted.has(row.id)) continue;
    const members =
      row.family && !isIndividuallyPinnedSibling(row) ? families.get(row.family.id)! : [row];
    for (const member of members) {
      if (!emitted.has(member.id)) result.push(member);
      emitted.add(member.id);
    }
  }
  return result;
}

export function isIndentedSibling(row: Conversation): boolean {
  return !!row.family?.representative_id && row.family.representative_id !== row.id;
}
