import type { Conversation } from "@/hooks/useConversations";

/** Retain each family's sorted position, but put its representative before its peers. */
export function orderSessionFamilies(rows: Conversation[]): Conversation[] {
  const byId = new Map(rows.map((row) => [row.id, row]));
  const emitted = new Set<string>();
  const result: Conversation[] = [];
  for (const row of rows) {
    if (emitted.has(row.id)) continue;
    const representative = row.family?.representative_id
      ? byId.get(row.family.representative_id)
      : undefined;
    const members = representative
      ? [
          representative,
          ...rows
            .filter(
              (other) => other.id !== representative.id && other.family?.id === row.family?.id,
            )
            .sort((a, b) => a.created_at - b.created_at || a.id.localeCompare(b.id)),
        ]
      : [row];
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
