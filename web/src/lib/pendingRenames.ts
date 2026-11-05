// A sidebar rename paints its title in place, but the search-indexed session
// list keeps serving the replaced title for a while. Until that reindex window
// passes, list rows that predate the rename get the newest title back. Each
// rename is tracked separately so overlapping renames settle independently.

import type { QueryClient } from "@tanstack/react-query";
import type { ConversationsInfiniteData } from "@/lib/sessionListCache";

/** How long the session list may lag a rename; matches the delete/archive tombstones. */
const RENAME_REINDEX_MS = 60_000;

/** One rename of a session, from submit until its reindex window passes. */
export interface PendingRename {
  readonly sequence: number;
  title: string | null;
  /** Titles a lagging list may still serve instead of this one. */
  readonly replaced: Set<string | null>;
  /** Title a failure restores; `undefined` when none was snapshotted. */
  restore: string | null | undefined;
  /** Server `updated_at` of the confirmed rename; newer rows are not lagging. */
  confirmedAt?: number;
  released: boolean;
  timer?: ReturnType<typeof setTimeout>;
}

/** Live renames per session, in submit order; the last one owns the title. */
const pendingRenames = new Map<string, PendingRename[]>();
let nextSequence = 0;

function holdRename(id: string, rename: PendingRename): void {
  if (rename.timer !== undefined) clearTimeout(rename.timer);
  rename.timer = setTimeout(() => dropRename(id, rename), RENAME_REINDEX_MS);
}

function dropRename(id: string, rename: PendingRename): void {
  if (rename.timer !== undefined) clearTimeout(rename.timer);
  const renames = (pendingRenames.get(id) ?? []).filter((other) => other !== rename);
  if (renames.length > 0) pendingRenames.set(id, renames);
  else pendingRenames.delete(id);
}

/**
 * Start keeping a rename's title over list rows that still carry a title it
 * replaced, including those of any earlier rename still pending.
 *
 * @param id - Session being renamed.
 * @param title - The requested title.
 * @param restore - Title a failed PATCH restores, if one was snapshotted.
 * @param replaced - Titles the caches held when the rename started.
 * @returns The rename, for settling it.
 */
export function startRename(
  id: string,
  title: string,
  restore: string | null | undefined,
  replaced: (string | null | undefined)[],
): PendingRename {
  const earlier = pendingRenames.get(id) ?? [];
  nextSequence += 1;
  const rename: PendingRename = {
    sequence: nextSequence,
    title,
    restore,
    replaced: new Set(),
    released: false,
  };
  for (const other of earlier) {
    rename.replaced.add(other.title);
    for (const value of other.replaced) rename.replaced.add(value);
  }
  for (const value of replaced) if (value !== undefined) rename.replaced.add(value);
  rename.replaced.delete(title);
  pendingRenames.set(id, [...earlier, rename]);
  // A PATCH that never settles must not hold the guard forever.
  holdRename(id, rename);
  return rename;
}

/** Whether a later rename of the session is still pending or held. */
export function isRenameSuperseded(id: string, rename: PendingRename): boolean {
  return (pendingRenames.get(id) ?? []).some((other) => other.sequence > rename.sequence);
}

/**
 * Record a rename the server confirmed and keep it for the reindex window
 * from now, even when its PATCH outlived the submit-time hold.
 */
export function confirmRename(
  id: string,
  rename: PendingRename,
  title: string | null,
  updatedAt: number,
): void {
  if (rename.released) return;
  rename.title = title;
  rename.confirmedAt = updatedAt;
  rename.replaced.delete(title);
  const renames = pendingRenames.get(id) ?? [];
  if (!renames.includes(rename)) {
    pendingRenames.set(
      id,
      [...renames, rename].sort((left, right) => left.sequence - right.sequence),
    );
  }
  holdRename(id, rename);
}

/** Stop keeping a rename the server rejected. */
export function failRename(id: string, rename: PendingRename): void {
  dropRename(id, rename);
  // A later rename that would restore this rejected title restores its predecessor instead.
  for (const later of pendingRenames.get(id) ?? []) {
    if (later.sequence > rename.sequence && later.restore === rename.title) {
      later.restore = rename.restore;
    }
  }
}

/** Stop keeping every rename of a session once its title is known to have changed since. */
export function releasePendingRenames(id: string): void {
  for (const rename of pendingRenames.get(id) ?? []) {
    rename.released = true;
    if (rename.timer !== undefined) clearTimeout(rename.timer);
  }
  pendingRenames.delete(id);
}

/** Wipe pending renames between tests. */
export function clearPendingRenames(): void {
  for (const id of [...pendingRenames.keys()]) releasePendingRenames(id);
}

function withPendingRenameTitles(
  data: ConversationsInfiniteData | undefined,
): ConversationsInfiniteData | undefined {
  if (!data || pendingRenames.size === 0) return data;
  let changed = false;
  const pages = data.pages.map((page) => {
    let pageChanged = false;
    const rows = page.data.map((row) => {
      const current = pendingRenames.get(row.id)?.at(-1);
      const title = row.title ?? null;
      if (current === undefined || title === current.title) return row;
      if (current.confirmedAt !== undefined && row.updated_at > current.confirmedAt) {
        releasePendingRenames(row.id);
        return row;
      }
      if (!current.replaced.has(title)) return row;
      pageChanged = true;
      return { ...row, title: current.title };
    });
    if (!pageChanged) return page;
    changed = true;
    return { ...page, data: rows };
  });
  return changed ? { ...data, pages } : data;
}

/**
 * Keep pending renames in every session-list cache, whichever write (a list
 * refetch, a stream frame, another mutation's rollback) brings an old title back.
 *
 * @returns The cache unsubscribe.
 */
export function keepPendingRenamesInLists(queryClient: QueryClient): () => void {
  return queryClient.getQueryCache().subscribe((event) => {
    if (pendingRenames.size === 0 || event.type !== "updated") return;
    if (event.action.type !== "success") return;
    const key = event.query.queryKey;
    if (key[0] !== "conversations" && key[0] !== "project-sessions") return;
    const data = event.query.state.data as ConversationsInfiniteData | undefined;
    const next = withPendingRenameTitles(data);
    if (next !== data) queryClient.setQueryData(key, next);
  });
}
