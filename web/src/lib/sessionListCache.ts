// Pure helpers for applying session-list push deltas (from the
// `WS /v1/sessions/updates` stream) into the TanStack Query cache that
// backs the sidebar's `["conversations", ...]` infinite queries.
//
// The merge / remove logic is pure so it can be unit-tested directly;
// `overlayTitleIntoCaches` takes the QueryClient as a parameter rather than
// reaching for a hook, keeping this module free of React runtime imports.
// Production callers: SessionUpdatesProvider (push deltas),
// useRenameConversation (overlaying the PATCH response after a rename), and
// the chat store's `session.title` handler (a terminal-side `/rename`).

import type { InfiniteData, QueryClient } from "@tanstack/react-query";
import type { Conversation, ConversationsPage } from "@/hooks/useConversations";
import type { Session } from "@/lib/types";

/** Cache value shape for a `useConversations` infinite query. */
export type ConversationsInfiniteData = InfiniteData<ConversationsPage, string | undefined>;

/**
 * The reserved `conversation_labels` key that stores a session's project
 * membership. Namespaced (`omni_*`) so it never collides with the user-facing
 * "project" term or other reserved keys. Defined in this leaf cache module so
 * membership checks can read it without importing back from the hooks layer
 * (which would create a value import cycle); `useConversations` re-exports it
 * for the existing consumers.
 */
export const PROJECT_LABEL_KEY = "omni_project";

/**
 * The reserved `conversation_labels` key that records whether a session is
 * pinned in the sidebar. The value is the epoch-ms timestamp of when it was
 * pinned (any non-empty value means pinned); the label is absent when unpinned
 * (the server deletes it on an empty-string PATCH). Storing the pin time lets
 * the Pinned section order by pin recency, stable when a new message bumps a
 * session's `updated_at`. Mirrors the server's `PINNED_LABEL_KEY`. Kept beside
 * `PROJECT_LABEL_KEY` in this leaf module so the sidebar can derive pin state
 * without a hooks-layer import cycle.
 */
export const PINNED_LABEL_KEY = "omnigent.pinned";

/** Filter dimensions encoded by a `["conversations", ...]` query key. */
export interface ConversationListFilters {
  searchQuery: string;
  includeArchived: boolean;
  /**
   * Project (``omni_project`` label) the list is scoped to. Present only on
   * the project-filtered variant (the Archived settings view's picker); the
   * default sidebar / search queries omit it entirely. ``undefined`` means
   * "all projects".
   */
  project?: string;
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
 * The base key is `["conversations", searchQuery, includeArchived]`. The
 * project-filtered variant appends a fourth element:
 * `["conversations", searchQuery, includeArchived, project]` (the Archived
 * settings picker is the only producer today). Both lengths are accepted so
 * the rename overlay and push-delta merge — which iterate *every* cached
 * `["conversations", ...]` query — never throw on the project variant. Query
 * membership decisions depend on these dimensions, so malformed keys fail
 * loudly instead of being guessed.
 *
 * @param key - TanStack Query key for a conversations query.
 * @returns Parsed list filters.
 * @throws Error if the key is not a conversations list key.
 */
export function filtersFromConversationQueryKey(key: readonly unknown[]): ConversationListFilters {
  if ((key.length !== 3 && key.length !== 4) || key[0] !== "conversations") {
    throw new Error("Invalid conversations query key");
  }
  const [, searchQuery, includeArchived, project] = key;
  if (typeof searchQuery !== "string" || typeof includeArchived !== "boolean") {
    throw new Error("Invalid conversations query key");
  }
  if (project !== undefined && typeof project !== "string") {
    throw new Error("Invalid conversations query key");
  }
  return {
    searchQuery,
    includeArchived,
    project,
  };
}

/**
 * Check membership rules the client can decide exactly from a patched row.
 *
 * - Archived rows never belong in default (non-includeArchived) queries.
 * - Project-filtered variants (the Archived picker's `["conversations","",true,
 *   name]` key) hold only rows whose `omni_project` label matches; a row
 *   relabeled out of that project — via a push-delta — is no longer a member.
 *   A falsy project (`undefined` or `""`) is the "all projects" list and
 *   applies no project constraint — consistent with the request (which omits
 *   `project=` for a falsy value) and the query key (which drops it). This list
 *   never requests the server's "unfiled" (`project=`) slice.
 *
 * @param conv - Cached row after applying the incoming wire item.
 * @param filters - Canonical filters for the query being patched.
 * @returns `true` when the row should be removed immediately.
 */
function violatesKnownMembership(conv: Conversation, filters: ConversationListFilters): boolean {
  if (!filters.includeArchived && conv.archived === true) return true;
  if (filters.project && conv.labels?.[PROJECT_LABEL_KEY] !== filters.project) return true;
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
  // A labels change can move a row between project-filtered variants and the
  // project folders (["project-sessions", …]). A session relabeled INTO the
  // selected project isn't in that filtered cache yet, so no local patch can
  // place it; only a server reconcile can. The unfiltered variant where the
  // row lives detects the label change here and flags the refetch, and the
  // caller's invalidation is prefix-wide (["conversations"]), so it reconciles
  // the filtered variants too.
  if (changed.has("labels")) return true;
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
 * Prepend brand-new rows (a create here or elsewhere, a share) to page 0 so the
 * sidebar shows them the instant the push lands, instead of after the debounced
 * refetch (which lags the search index). A new row sorts newest-first, so page 0
 * is its home. Skips: search lists (membership unknown), archived/wrong-project
 * rows, sub-agent children (`parent_session_id` — they live off the sidebar),
 * and ids the caller excludes via `skip` (e.g. an optimistic delete in flight).
 * `candidates` should already exclude rows the list holds.
 */
export function insertNewRowsIntoPages(
  data: ConversationsInfiniteData | undefined,
  candidates: Map<string, SessionListWireItem>,
  filters: ConversationListFilters,
  skip?: (id: string) => boolean,
): { data: ConversationsInfiniteData | undefined; inserted: Set<string> } {
  const inserted = new Set<string>();
  if (!data || candidates.size === 0 || filters.searchQuery) return { data, inserted };
  const present = new Set<string>();
  for (const page of data.pages) for (const c of page.data) present.add(c.id);
  const rows: Conversation[] = [];
  for (const [id, wire] of candidates) {
    if (present.has(id) || skip?.(id)) continue;
    const conv: Conversation = {
      object: "conversation",
      title: null,
      created_at: 0,
      updated_at: 0,
      labels: {},
      permission_level: null,
      ...nullsToUndefined(wire),
      id,
    };
    if (conv.parent_session_id != null || violatesKnownMembership(conv, filters)) continue;
    rows.push(conv);
    inserted.add(id);
  }
  if (rows.length === 0) return { data, inserted };
  const [first, ...rest] = data.pages;
  const nextFirst = { ...first, data: [...rows, ...first.data], first_id: rows[0].id };
  return { data: { ...data, pages: [nextFirst, ...rest] }, inserted };
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
export function collectConversationIds(datas: (ConversationsInfiniteData | undefined)[]): string[] {
  const ids = new Set<string>();
  for (const data of datas) {
    if (!data) continue;
    for (const page of data.pages) {
      for (const conv of page.data) ids.add(conv.id);
    }
  }
  return [...ids];
}

/**
 * Filters for a project-folder list (`["project-sessions", name]`) — those
 * lists are non-archived and unsearched, the same dimensions the push-delta
 * merge uses when overlaying rows into them.
 */
export const PROJECT_FOLDER_FILTERS = { searchQuery: "", includeArchived: false } as const;

/**
 * Overlay a title into every cache that holds a row for one session.
 *
 * Only the fields a rename changes are written — a full session snapshot
 * carries nulls for absent fields that would clobber list-shaped rows (see
 * {@link nullsToUndefined}). Covers the flat `["conversations"]` lists, the
 * per-project `["project-sessions"]` lists (a filed session renders from its
 * own list, so skipping it leaves the folder row stale), the pinned-row
 * `["conversation-backfill"]` cache, and the per-session `["session"]`
 * snapshot — the last two have long stale times and would otherwise serve
 * the old title long after.
 *
 * Shared by the two paths that learn a title changed outside a list fetch:
 * `useRenameConversation` (optimistic paint for a Web UI rename) and the
 * `session.title` SSE handler (a `/rename` typed in a native terminal).
 *
 * @param queryClient - The app QueryClient.
 * @param id - Conversation id whose title changed.
 * @param title - The new title, or `null` to clear it.
 * @param updatedAt - Optional `updated_at` to write alongside the title.
 */
export function overlayTitleIntoCaches(
  queryClient: QueryClient,
  id: string,
  title: string | null,
  updatedAt?: number,
): void {
  const wire: SessionListWireItem = {
    id,
    title,
    ...(updatedAt !== undefined ? { updated_at: updatedAt } : {}),
  };
  const itemsById = new Map([[id, wire]]);
  for (const [key, data] of queryClient.getQueriesData<ConversationsInfiniteData>({
    queryKey: ["conversations"],
  })) {
    // activeId only gates `needsRefetch`, which both callers ignore —
    // they patch in place rather than refetching.
    const { data: next } = mergeItemsIntoPages(
      data,
      itemsById,
      filtersFromConversationQueryKey(key),
      undefined,
    );
    if (next !== data) queryClient.setQueryData(key, next);
  }
  for (const [key, data] of queryClient.getQueriesData<ConversationsInfiniteData>({
    queryKey: ["project-sessions"],
  })) {
    const { data: next } = mergeItemsIntoPages(data, itemsById, PROJECT_FOLDER_FILTERS, undefined);
    if (next !== data) queryClient.setQueryData(key, next);
  }
  queryClient.setQueryData<Conversation | null>(["conversation-backfill", id], (old) =>
    old ? { ...old, title, ...(updatedAt !== undefined ? { updated_at: updatedAt } : {}) } : old,
  );
  queryClient.setQueryData<Session>(["session", id], (old) => (old ? { ...old, title } : old));
}

/**
 * Overlay an archived-flag change onto every cached list that holds the row,
 * so archiving repaints the sidebar on the next frame instead of after the
 * PATCH round-trips (which is what made archive feel slower than delete).
 * Mirrors {@link overlayTitleIntoCaches}: the merge keeps the row in an
 * include-archived list (marked archived, which the sidebar filters out
 * client-side) and drops it from a non-archived project folder via
 * `violatesKnownMembership`.
 *
 * Patched in place rather than invalidated, for the same reason rename/delete
 * are: GET /v1/sessions may be served from a search index that lags the PATCH,
 * so an immediate refetch races the reindex and bounces the row back. The
 * server-confirmed state converges via the WS stream and the reconcile poll.
 *
 * ponytail: a reconcile poll firing inside the reindex-lag window (before the
 * index reflects the archive) can briefly bounce the row back; it self-heals on
 * the next poll. Add a fetch-time flag override (like `withoutDeletingSessions`)
 * if metrics show the bounce.
 */
export function overlayArchivedIntoCaches(
  queryClient: QueryClient,
  id: string,
  archived: boolean,
): void {
  const itemsById = new Map<string, SessionListWireItem>([[id, { id, archived }]]);
  for (const [key, data] of queryClient.getQueriesData<ConversationsInfiniteData>({
    queryKey: ["conversations"],
  })) {
    const { data: next } = mergeItemsIntoPages(
      data,
      itemsById,
      filtersFromConversationQueryKey(key),
      undefined,
    );
    if (next !== data) queryClient.setQueryData(key, next);
  }
  for (const [key, data] of queryClient.getQueriesData<ConversationsInfiniteData>({
    queryKey: ["project-sessions"],
  })) {
    const { data: next } = mergeItemsIntoPages(data, itemsById, PROJECT_FOLDER_FILTERS, undefined);
    if (next !== data) queryClient.setQueryData(key, next);
  }
  queryClient.setQueryData<Conversation | null>(["conversation-backfill", id], (old) =>
    old ? { ...old, archived } : old,
  );
}
