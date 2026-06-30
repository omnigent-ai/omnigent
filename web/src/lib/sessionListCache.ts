// Pure helpers for applying session-list push deltas (from the
// `WS /v1/sessions/updates` stream) into the TanStack Query cache that
// backs the sidebar's `["conversations", ...]` infinite queries.
//
// Kept side-effect-free and React-free so the merge / remove logic can be
// unit-tested directly. Production callers wire these to the real
// QueryClient: SessionUpdatesProvider (push deltas) and
// useRenameConversation (overlaying the PATCH response after a rename).

import type { InfiniteData } from "@tanstack/react-query";
import type { Conversation, ConversationsPage } from "@/hooks/useConversations";

/** Cache value shape for a `useConversations` infinite query. */
export type ConversationsInfiniteData = InfiniteData<ConversationsPage, string | undefined>;

/** Filter dimensions encoded by a `["conversations", ...]` query key. */
export interface ConversationListFilters {
  searchQuery: string;
  includeArchived: boolean;
}

/**
 * A `SessionListItem` as it arrives on the wire in `snapshot` / `changed`
 * frames. Field names match {@link Conversation}, so an item overlays a
 * cached row directly. Always carries at least an `id`.
 */
export type SessionListWireItem = Partial<Conversation> & { id: string };

/**
 * Convert a wire item's `null` values to `undefined`, preserving keys.
 *
 * The session-updates stream sends full rows (every field, nulls included) so
 * a field that cleared to null arrives explicitly rather than being dropped —
 * which is what lets the overlay merge clear it. But the rest of the app reads
 * these rows in the shape `GET /v1/sessions` produces, where an empty field is
 * *absent* (`undefined`), not `null`. Most consumers use `?? ` and don't care,
 * but the sidebar's `permission_level === null` full-access sentinel does: a
 * streamed `permission_level: null` would wrongly flip a row to owner/edit/
 * manage. Converting null → undefined here keeps streamed rows in the list's
 * shape, so a cleared field reads as empty and the sentinel is never tripped.
 *
 * @param wire - A wire item from a snapshot/changed frame.
 * @returns A copy with every `null` value replaced by `undefined`; keys are
 *   kept (with `undefined` values) so the diff still sees a cleared field.
 */
export function nullsToUndefined(wire: SessionListWireItem): SessionListWireItem {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(wire)) {
    out[key] = value === null ? undefined : value;
  }
  return out as SessionListWireItem;
}

/**
 * Return the wire fields whose values differ from the cached row.
 *
 * Lets the merge skip rewriting rows that are already up to date, so a
 * snapshot frame (which restates the whole watch-set, mostly unchanged)
 * doesn't churn the cache and re-render the sidebar. `labels` is compared
 * structurally because every parsed frame yields a fresh object reference.
 *
 * @param conv - The cached conversation row.
 * @param wire - The incoming wire item overlaying it.
 * @returns Field names that would change if `wire` were applied.
 */
function changedWireFields(conv: Conversation, wire: SessionListWireItem): Set<string> {
  const changed = new Set<string>();
  // Index the typed Conversation by arbitrary wire keys to compare field
  // by field. The double cast is required because Conversation has no index
  // signature; the wire item's keys are a subset of Conversation's fields.
  const row = conv as unknown as Record<string, unknown>;
  for (const [key, value] of Object.entries(wire)) {
    const current = row[key];
    if (key === "labels") {
      if (JSON.stringify(current) !== JSON.stringify(value)) changed.add(key);
    } else if (current !== value) {
      changed.add(key);
    }
  }
  return changed;
}

/**
 * Decode the filter dimensions from a conversations query key.
 *
 * The canonical key is `["conversations", searchQuery, includeArchived]`.
 * Query membership decisions depend on both filter dimensions, so
 * malformed keys fail loudly instead of being guessed.
 *
 * @param key - TanStack Query key for a conversations query.
 * @returns Parsed list filters.
 * @throws Error if the key is not the canonical conversations list key.
 */
export function filtersFromConversationQueryKey(key: readonly unknown[]): ConversationListFilters {
  if ((key.length !== 3 && key.length !== 4) || key[0] !== "conversations") {
    throw new Error("Invalid conversations query key");
  }
  const [, searchQuery, includeArchived] = key;
  if (typeof searchQuery !== "string" || typeof includeArchived !== "boolean") {
    throw new Error("Invalid conversations query key");
  }
  return {
    searchQuery,
    includeArchived,
  };
}

/**
 * Check membership rules the client can decide exactly from a patched row.
 *
 * Archived rows never belong in default (non-includeArchived) queries.
 *
 * @param conv - Cached row after applying the incoming wire item.
 * @param filters - Canonical filters for the query being patched.
 * @returns `true` when the row should be removed immediately.
 */
function violatesKnownMembership(conv: Conversation, filters: ConversationListFilters): boolean {
  if (!filters.includeArchived && conv.archived === true) return true;
  return false;
}

/**
 * Decide whether a field change needs server-side list reconciliation.
 *
 * The server owns pagination, updated_at sorting, search matches over title
 * and item content, and archive filtering. Push frames update visible row
 * fields immediately; this tells the provider when to follow with a list
 * refetch so filtered query membership and page order converge. Title
 * changes reconcile every list variant because a row absent from a search
 * query may now match it.
 *
 * @param changed - Names of wire fields that changed the cached row.
 * @param isActiveRow - Whether this row is the active chat (the one held
 *   in place by `ActiveChatOverride`). An `updated_at`-only change on it
 *   doesn't move the visible row, so it doesn't need a server resort.
 * @returns `true` when the query should be invalidated after patching.
 */
function changedFieldsNeedRefetch(changed: Set<string>, isActiveRow: boolean): boolean {
  if (changed.has("archived")) return true;
  if (changed.has("title")) return true;
  // updated_at only affects the server's sort order. The active chat row is
  // pinned at its position by ActiveChatOverride regardless of that order, so
  // an updated_at bump on it — the common case while the user sends messages —
  // never changes what's visible. Skip the full-list refetch it would
  // otherwise force every tick. Any other row's updated_at still needs the
  // server resort to move it.
  if (changed.has("updated_at") && !isActiveRow) return true;
  return false;
}

/**
 * Overlay incoming wire items onto matching rows of one infinite query's
 * cached pages.
 *
 * Only rows whose id appears in `itemsById` are touched, and only when a
 * value actually changed — pages and the top-level object keep their old
 * reference when nothing changed, so the caller can skip the
 * `setQueryData` (and the re-render) entirely. Items whose id isn't present
 * in any page are reported back via `found` so the caller can treat them as
 * structural additions (a debounced refetch), rather than guessing where to
 * insert them in the server's sort order.
 *
 * @param data - The cached infinite data, or `undefined` for an unfetched
 *   query.
 * @param itemsById - Wire items keyed by conversation id.
 * @param filters - Canonical filters for this conversations query.
 * @param activeId - The active chat's conversation id (`/c/:id`), or
 *   `undefined` when not on a chat route. Its `updated_at` bumps don't force
 *   a refetch because `ActiveChatOverride` pins its visible position.
 * @returns The possibly updated data, ids found in it, and whether this
 *   query needs a server refetch after the local patch.
 */
export function mergeItemsIntoPages(
  data: ConversationsInfiniteData | undefined,
  itemsById: Map<string, SessionListWireItem>,
  filters: ConversationListFilters,
  activeId: string | undefined,
): { data: ConversationsInfiniteData | undefined; found: Set<string>; needsRefetch: boolean } {
  const found = new Set<string>();
  if (!data) return { data, found, needsRefetch: false };
  let anyPageChanged = false;
  let needsRefetch = false;
  const pages = data.pages.map((page) => {
    let rowChanged = false;
    const nextData: Conversation[] = [];
    for (const conv of page.data) {
      const wire = itemsById.get(conv.id);
      if (!wire) {
        nextData.push(conv);
        continue;
      }
      found.add(conv.id);
      const changed = changedWireFields(conv, wire);
      if (changed.size === 0) {
        nextData.push(conv);
        continue;
      }
      const nextConv = { ...conv, ...wire };
      if (violatesKnownMembership(nextConv, filters)) {
        rowChanged = true;
        needsRefetch = true;
        continue;
      }
      if (changedFieldsNeedRefetch(changed, conv.id === activeId)) {
        needsRefetch = true;
      }
      rowChanged = true;
      nextData.push(nextConv);
    }
    if (!rowChanged) return page;
    anyPageChanged = true;
    return { ...page, data: nextData };
  });
  if (!anyPageChanged) return { data, found, needsRefetch };
  return { data: { ...data, pages }, found, needsRefetch };
}

/**
 * Drop rows with the given ids from one infinite query's cached pages.
 *
 * Page cursors are recomputed from the surviving rows: `last_id` of the
 * final page is the `after=` anchor `fetchNextPage` sends, and a deleted
 * anchor id makes the server's keyset lookup miss (the next page comes
 * back empty). An emptied page gets null cursors — infinite scroll then
 * pauses until the next reconcile refetch rebuilds the pages, which
 * beats paginating from a dead anchor.
 *
 * @param data - The cached infinite data, or `undefined`.
 * @param ids - Conversation ids to remove.
 * @returns The (possibly identical) data and whether anything was removed.
 */
export function removeIdsFromPages(
  data: ConversationsInfiniteData | undefined,
  ids: Set<string>,
): { data: ConversationsInfiniteData | undefined; removed: boolean } {
  if (!data || ids.size === 0) return { data, removed: false };
  let changed = false;
  const pages = data.pages.map((page) => {
    const nextData = page.data.filter((conv) => !ids.has(conv.id));
    if (nextData.length === page.data.length) return page;
    changed = true;
    return {
      ...page,
      data: nextData,
      first_id: nextData[0]?.id ?? null,
      last_id: nextData[nextData.length - 1]?.id ?? null,
    };
  });
  if (!changed) return { data, removed: false };
  return { data: { ...data, pages }, removed: true };
}

/**
 * Collect every conversation id present across a set of infinite-query
 * caches — the union forms the stream's watch-set.
 *
 * @param datas - Cached infinite data for each `["conversations", ...]`
 *   query variant (base, search, archived). `undefined` entries are
 *   skipped.
 * @returns Deduplicated conversation ids.
 */
export function collectConversationIds(
  datas: Array<ConversationsInfiniteData | undefined>,
): string[] {
  const ids = new Set<string>();
  for (const data of datas) {
    if (!data) continue;
    for (const page of data.pages) {
      for (const conv of page.data) ids.add(conv.id);
    }
  }
  return [...ids];
}
