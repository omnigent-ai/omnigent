// Where the app goes after archiving the open session: the most recently visited entry that is
// still live, then the rest in sidebar order, as ⌃[ walks them. New session only when nothing is
// left, and no choice at all while the session list is still loading or failed to load.

/** How far up a session's parents a subtree check climbs, like the other ancestor walks. */
const MAX_PARENT_HOPS = 32;

export type ArchiveLanding =
  | { kind: "entry"; entryId: string; landed: "recent" | "sidebar_order" }
  | { kind: "new_session" }
  /** The session list hasn't loaded: wait and choose again once it has. */
  | { kind: "pending" }
  /** The session list failed to load: an empty list proves nothing, so don't choose. */
  | { kind: "list_failed" };

export interface ArchiveLandingInputs {
  /** Visited entries, most recent first. */
  history: readonly string[];
  /** Every sidebar entry, in sidebar order. */
  sidebarEntryIds: readonly string[];
  /** Whether an entry is live now: not archived or deleted. */
  isLive: (entryId: string) => boolean;
  /** An entry's parent session, or null for a top-level session. */
  parentOf: (entryId: string) => string | null;
  listState: "loading" | "loaded" | "failed";
}

/** The nearest of `leavingIds` among `entryId` and its parents, or null when none is leaving. */
export function leavingAncestor(
  entryId: string,
  leavingIds: ReadonlySet<string>,
  parentOf: (entryId: string) => string | null,
): string | null {
  let id: string | null = entryId;
  for (let hop = 0; id !== null && hop <= MAX_PARENT_HOPS; hop++) {
    if (leavingIds.has(id)) return id;
    id = parentOf(id);
  }
  return null;
}

/** Whether `entryId` is one of `leavingIds` or sits anywhere under one of them. */
export function isInLeavingSubtree(
  entryId: string,
  leavingIds: ReadonlySet<string>,
  parentOf: (entryId: string) => string | null,
): boolean {
  return leavingAncestor(entryId, leavingIds, parentOf) !== null;
}

/** The entry to open after archiving `leavingIds`, or why there isn't one yet. */
export function archiveLanding(
  leavingIds: ReadonlySet<string>,
  { history, sidebarEntryIds, isLive, parentOf, listState }: ArchiveLandingInputs,
): ArchiveLanding {
  const eligible = (entryId: string) =>
    isLive(entryId) && !isInLeavingSubtree(entryId, leavingIds, parentOf);
  // A visit known to be live can be opened before the whole list has loaded.
  const recent = history.find(eligible);
  if (recent !== undefined) return { kind: "entry", entryId: recent, landed: "recent" };
  if (listState === "loading") return { kind: "pending" };
  if (listState === "failed") return { kind: "list_failed" };
  const next = sidebarEntryIds.find(eligible);
  if (next !== undefined) return { kind: "entry", entryId: next, landed: "sidebar_order" };
  return { kind: "new_session" };
}
