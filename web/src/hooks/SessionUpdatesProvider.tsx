import { SidebarDataContext } from "./useSidebarData";
import { PINNED_CONVERSATIONS_KEY, type PinnedConversationsResult } from "./useConversations";
// App-level wiring for the `WS /v1/sessions/updates` push stream.
//
// Mounted once near the app root. It:
//   1. opens the shared session-updates WebSocket for the tab's lifetime,
//   2. derives the watch-set from whatever conversation ids are currently
//      cached across the sidebar's `["conversations", ...]` query variants,
//      and pushes it to the socket, and
//   3. applies incoming snapshot/changed/removed frames back into that cache
//      — patching fields in place, placing new rows and evicting departed
//      ones locally, and falling back to a debounced refetch only for lists
//      whose membership the server decides (search, archived,
//      project-filtered) or a folder the client can't resolve.
//
// This replaces the old 4 s list poll; `useConversations` keeps low-rate
// HTTP reconciliation so new sessions from other tabs / CLIs are still
// discovered while the socket handles watched-row freshness.

import { type ReactNode, useCallback, useContext, useEffect, useRef } from "react";
import { type Query, type QueryClient, type QueryKey, useQueryClient } from "@tanstack/react-query";
import { getCurrentUserId } from "@/lib/identity";
import { useActiveConversationId } from "@/hooks/useActiveConversationId";
import { childSessionsQueryKey, type ChildSessionInfo } from "@/hooks/useChildSessions";
import {
  isSessionArchiving,
  isSessionDeleting,
  markRecentlyCreated,
  type ProjectSummary,
} from "@/hooks/useConversations";
import {
  type ConversationListFilters,
  type ConversationsInfiniteData,
  type SessionListWireItem,
  collectConversationIds,
  filtersFromConversationQueryKey,
  firstPageOnly,
  insertNewRowsIntoPages,
  mergeItemsIntoPages,
  nullsToUndefined,
  PROJECT_FOLDER_FILTERS,
  removeIdsFromPages,
  serverDecidesMembership,
} from "@/lib/sessionListCache";
import { isModalHostResolved, resolveModalHost } from "@/lib/sessionHost";
import { type SessionUpdatesFrame, sessionUpdatesSocket } from "@/lib/sessionUpdatesSocket";
import { isTempConvId } from "@/lib/tempConversationId";

// Coalesce bursts of structural changes / watch-set recomputes into one
// action. 250 ms is short enough to feel live, long enough to batch the
// flurry of cache writes a single frame can trigger.
const DEBOUNCE_MS = 250;

/**
 * Overlay wire items onto every cached `["conversations", ...]` variant and
 * project folder, placing rows the client can position itself.
 *
 * @param queryClient - The app QueryClient.
 * @param items - Wire items from a snapshot/changed frame.
 * @param viewerId - Current user, for ownership-scoped lists.
 * @returns Top-level ids no cached list held before this frame, whether a
 *   membership-deciding field changed, folders the client couldn't resolve
 *   (they reconcile with the server instead), and hashes of lists that were
 *   mid-fetch (their result is built from pre-frame pages).
 */
function applyItemsToCache(
  queryClient: QueryClient,
  items: SessionListWireItem[],
  viewerId?: string | null,
): {
  missingIds: string[];
  needsRefetch: boolean;
  unresolvedFolders: QueryKey[];
  fetchingLists: string[];
} {
  // Frames are full rows with explicit nulls; convert null → undefined so a
  // cleared field overlays the cache in the same shape GET /v1/sessions
  // produces (absent), without tripping the permission_level === null sentinel.
  const itemsById = new Map<string, SessionListWireItem>();
  for (const item of items) {
    if (isSessionDeleting(item.id)) continue;
    itemsById.set(
      item.id,
      nullsToUndefined(isSessionArchiving(item.id) ? { ...item, archived: true } : item),
    );
  }
  const foundAnywhere = new Set<string>();
  let needsRefetch = false;
  const fetchingLists: string[] = [];
  // Merge and place one cached list. A new watched session (a create here or
  // elsewhere, a share) shows now instead of after a refetch that lags the
  // search index; rows sorting onto an unloaded page are left for that page.
  const applyToList = (query: Query, filters: ConversationListFilters, place: boolean) => {
    const key = query.queryKey;
    const data = query.state.data as ConversationsInfiniteData | undefined;
    if (query.state.fetchStatus === "fetching") fetchingLists.push(query.queryHash);
    const {
      data: merged,
      found,
      needsRefetch: queryNeedsRefetch,
    } = mergeItemsIntoPages(data, itemsById, filters);
    for (const id of found) foundAnywhere.add(id);
    if (queryNeedsRefetch) needsRefetch = true;
    let next = merged;
    if (place) {
      const missingHere = new Map([...itemsById].filter(([id]) => !found.has(id)));
      const placed = insertNewRowsIntoPages(
        merged,
        missingHere,
        filters,
        isSessionDeleting,
        viewerId,
      );
      next = placed.data;
      // Keep the new row in the list-fetch until the search index catches up,
      // so a reconcile can't drop it before it's queryable — otherwise it
      // flashes in and out. Mirrors the delete tombstone.
      for (const row of placed.inserted) markRecentlyCreated(row);
    }
    if (next !== data) queryClient.setQueryData(key, next);
  };
  const cache = queryClient.getQueryCache();
  for (const query of cache.findAll({ queryKey: ["conversations"] })) {
    if (query.queryKey[4] === "archived") continue;
    applyToList(query, filtersFromConversationQueryKey(query.queryKey), true);
  }
  // Each project folder fetches its own ["project-sessions", <name>] list, so
  // streamed field updates (pending_elicitations_count → "Needs response",
  // status, runner_online, …) must overlay those caches too — otherwise a
  // filed session's row stays frozen at fetch time. With every project the
  // frame mentions cached, membership is decided locally like the sidebar's
  // grouping; a project created or promoted elsewhere leaves it to the server.
  // Folders hold only the viewer's own sessions, so another owner's private
  // project id can't make them ambiguous.
  const projects = queryClient.getQueryData<ProjectSummary[]>(["projects"]) ?? [];
  const knownProjectIds = new Set(projects.map((p) => p.id));
  const projectsKnown = [...itemsById.values()].every(
    (item) =>
      item.project_id == null ||
      knownProjectIds.has(item.project_id) ||
      (item.owner != null && item.owner !== viewerId),
  );
  const unresolvedFolders: QueryKey[] = [];
  for (const query of cache.findAll({ queryKey: ["project-sessions"] })) {
    const name = query.queryKey[1] as string;
    const sortBy = query.queryKey[2] === "created_at" ? "created_at" : undefined;
    const project = projectsKnown ? projects.find((p) => p.name === name) : undefined;
    if (!project) unresolvedFolders.push(query.queryKey);
    applyToList(
      query,
      {
        ...PROJECT_FOLDER_FILTERS,
        ...(project ? { visibility: "mine", folder: project } : {}),
        sortBy,
      },
      project !== undefined,
    );
  }
  queryClient.setQueryData<PinnedConversationsResult>(PINNED_CONVERSATIONS_KEY, (previous) => {
    if (!previous) return previous;
    let changed = false;
    const conversations = previous.conversations
      .map((row) => {
        const item = itemsById.get(row.id);
        if (!item) return row;
        foundAnywhere.add(row.id);
        changed = true;
        // Pin membership and pin timestamps reconcile through the pinned query.
        return {
          ...row,
          ...item,
          labels: {
            ...(item.labels ?? row.labels),
            ["omnigent.pinned"]: row.labels["omnigent.pinned"],
          },
        };
      })
      .filter((row) => !row.archived);
    return changed ? { ...previous, conversations } : previous;
  });
  return {
    // Sub-agent children are never list rows (the endpoint lists
    // `kind=default`), so a watched child absent from every page is expected.
    missingIds: [...itemsById.values()]
      .filter((item) => !foundAnywhere.has(item.id) && item.parent_session_id == null)
      .map((item) => item.id),
    needsRefetch,
    unresolvedFolders,
    fetchingLists,
  };
}

/**
 * Remove ids from every cached `["conversations", ...]` variant.
 *
 * @param queryClient - The app QueryClient.
 * @param ids - Conversation ids to evict.
 * @returns Whether any cached page changed, and lists whose removed rows took
 *   the next page's cursor with them (they can't page further until refetched).
 */
function removeIdsFromCache(
  queryClient: QueryClient,
  ids: string[],
): { removedAny: boolean; stranded: QueryKey[] } {
  const idSet = new Set(ids);
  let removedAny = false;
  const stranded: QueryKey[] = [];
  // Both the global lists and each project folder's own list (same page shape).
  for (const queryKey of [["conversations"], ["project-sessions"]]) {
    const entries = queryClient.getQueriesData<ConversationsInfiniteData>({ queryKey });
    for (const [key, data] of entries) {
      const { data: next, removed } = removeIdsFromPages(data, idSet);
      if (removed) {
        queryClient.setQueryData(key, next);
        removedAny = true;
        const tail = next?.pages.at(-1);
        if (tail?.has_more && !tail.last_id) stranded.push(key);
      }
    }
  }
  return { removedAny, stranded };
}

/**
 * Open the session-updates stream and keep the conversations cache in sync
 * with it for as long as the app is mounted.
 *
 * @param children - The app subtree.
 */
export function SessionUpdatesProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const sidebarData = useContext(SidebarDataContext);
  const identityReady = sidebarData?.identityReady ?? true;
  const sidebarIds = sidebarData?.watchedIds;
  const sidebarIdsRef = useRef(sidebarIds);
  sidebarIdsRef.current = sidebarIds;

  // Last comments fingerprint (`count:updated_at`) seen per session id.
  // A frame whose fingerprint differs from the recorded one means a comment
  // was added, edited, addressed, or deleted somewhere else (another user,
  // the agent's update_comment tool), so the cached comment list is stale.
  // First sight of a session also invalidates: a mutation landing between
  // the comments query's fetch and the socket's first frame would otherwise
  // be swallowed as "baseline" and never surface (a real race on page load
  // when the WS connects slowly). With no comments query mounted the
  // invalidation is a no-op, so the extra firing is free. The map survives
  // socket reconnects, so changes missed while disconnected are caught by
  // the post-reconnect snapshot.
  const commentsFingerprintsRef = useRef(new Map<string, string>());

  // The frame handler is installed once (effect deps are [queryClient]) but
  // the active route changes over the socket's lifetime. Keep the current
  // active id in a ref so the handler always reads the latest without
  // re-subscribing the socket on every navigation.
  const activeId = useActiveConversationId();
  const activeIdRef = useRef(activeId);
  activeIdRef.current = activeId;

  // Recompute the watch-set from the cache and push it to the socket.
  // setWatched no-ops when the id-set is unchanged, so in-place field
  // patches (and navigation between already-watched sessions) don't bounce
  // a new watch-set back to the server.
  const pushWatched = useCallback(() => {
    const entries = queryClient.getQueriesData<ConversationsInfiniteData>({
      queryKey: ["conversations"],
    });
    // Project folders fetch their members into their own caches; include those
    // ids so the server streams liveness (e.g. pending-elicitation "Needs
    // response") for filed sessions that aren't in the global loaded window.
    const projectEntries = queryClient.getQueriesData<ConversationsInfiniteData>({
      queryKey: ["project-sessions"],
    });
    const ids = sidebarIdsRef.current
      ? [...sidebarIdsRef.current]
      : collectConversationIds([...entries, ...projectEntries].map(([, data]) => data));
    // Union in the open session. A directly-opened child / sub-agent
    // session is filtered out of the sidebar list, so it's absent from
    // every cached conversations page and wouldn't otherwise be watched —
    // leaving the open-session view with no streamed liveness for it.
    // Adding it here makes the stream push runner_online / host_online for
    // the open session even when it's off-sidebar.
    const active = activeIdRef.current;
    if (active && !ids.includes(active)) ids.push(active);
    // Include all child session ids from the sub-agent tree caches so the
    // server streams status changes for them. When a child's changed frame
    // arrives below, we invalidate its parent's child_sessions query key,
    // giving the tree views push-style freshness without polling.
    const childEntries = queryClient.getQueriesData<ChildSessionInfo[]>({
      queryKey: ["conversation"],
    });
    for (const [key, childSessions] of childEntries) {
      // Only ["conversation", <id>, "child_sessions"] entries carry child lists.
      if (Array.isArray(key) && key[2] === "child_sessions" && Array.isArray(childSessions)) {
        for (const child of childSessions) {
          if (!ids.includes(child.id)) ids.push(child.id);
        }
      }
    }
    sessionUpdatesSocket.setWatched(ids.filter((id) => !isTempConvId(id)));
  }, [queryClient]);

  // Navigating to an off-sidebar child changes the open session without
  // touching the conversations cache, so the cache subscription below
  // won't fire. Re-push on activeId change so the open session joins the
  // watch-set (and an already-watched id no-ops via setWatched).
  useEffect(() => {
    pushWatched();
  }, [pushWatched, activeId, sidebarIds]);

  // Freeze the fallback slice key (the modal host over all seen sessions) once
  // the session list first loads, then start the updates socket — which keys
  // its `?omnigent_slice_key=` off that frozen value. Gating start() on the
  // latch (rather than starting eagerly) matters ONLY for this WS: it's a
  // persistent connection, so starting pre-latch (empty map → no key) and then
  // re-keying would force a reconnect. Ordinary `authenticatedFetch` fallback
  // routes (e.g. /v1/hosts) don't gate — they just read the frozen value and a
  // pre-latch miss self-corrects on their next refetch.
  //
  // Latch on the first SUCCESSFUL conversations query, not merely a settled one:
  // the queryFn seeds `_sessionHosts` from every row before it resolves, so a
  // success means the map already reflects this user's hosts (an empty map is
  // then a genuinely zero-session user → null fallback, gate still releases). An
  // error settle is NOT latched — freezing a null key there would pin the page
  // even after a retry loads real hosts; react-query retries the list, and the
  // socket starts on that first success (nothing to stream before the list loads
  // anyway). The session list itself is NOT gated on the modal (it populates the
  // map the modal reads; gating it on the modal would deadlock).
  useEffect(() => {
    if (!identityReady) return;
    let cacheUnsub: (() => void) | null = null;
    const tryResolveAndStart = (): boolean => {
      const succeeded = queryClient
        .getQueryCache()
        .getAll()
        .some(
          (q) =>
            Array.isArray(q.queryKey) &&
            q.queryKey[0] === "conversations" &&
            q.state.status === "success",
        );
      if (!succeeded) return false;
      resolveModalHost();
      sessionUpdatesSocket.start();
      return true;
    };
    // Already succeeded (cache warm from a prior mount)? Start immediately.
    // Otherwise wait for the first success, then start once and unsubscribe.
    if (!tryResolveAndStart()) {
      cacheUnsub = queryClient.getQueryCache().subscribe(() => {
        if (isModalHostResolved() || tryResolveAndStart()) cacheUnsub?.();
      });
    }

    let invalidateTimer: ReturnType<typeof setTimeout> | null = null;
    let membershipStale = false;
    let searchStale = false;
    const staleLists = new Map<string, QueryKey>();
    // Lists the client places rows into locally (the sidebar scopes and
    // resolved folders) aren't refetched; the low-rate reconcile poll covers
    // what the stream can't see.
    const scheduleInvalidate = (stale: {
      membership?: boolean;
      search?: boolean;
      lists?: QueryKey[];
    }) => {
      membershipStale ||= stale.membership ?? false;
      searchStale ||= stale.search ?? false;
      for (const key of stale.lists ?? []) staleLists.set(JSON.stringify(key), key);
      if (invalidateTimer !== null) return;
      invalidateTimer = setTimeout(() => {
        invalidateTimer = null;
        const membership = membershipStale;
        const search = searchStale;
        membershipStale = false;
        searchStale = false;
        if (membership || search) {
          void queryClient.invalidateQueries({
            queryKey: ["conversations"],
            predicate: (query) => {
              if (query.meta?.snapshot === true) return false;
              const filters = filtersFromConversationQueryKey(query.queryKey);
              return membership ? serverDecidesMembership(filters) : filters.searchQuery !== "";
            },
          });
        }
        for (const queryKey of staleLists.values()) {
          // An infinite query refetches every loaded page; reconcile the head
          // only. Cancel first so an in-flight page can't restore the trimmed ones.
          void queryClient.cancelQueries({ queryKey, exact: true }).then(() => {
            queryClient.setQueryData<ConversationsInfiniteData>(queryKey, firstPageOnly);
            void queryClient.invalidateQueries({ queryKey, exact: true });
          });
        }
        staleLists.clear();
        if (membership) {
          // Another client archiving/relabeling/deleting can add or remove a
          // project from the Archived view's picker; only local mutations
          // invalidate this scan otherwise.
          void queryClient.invalidateQueries({ queryKey: ["archived-project-names"] });
        }
      }, DEBOUNCE_MS);
    };

    // An infinite-query fetch builds its result from the pages it started
    // with, so a frame applied mid-fetch is lost when the fetch lands. Replay
    // such frames once those lists settle; re-applying a frame is idempotent.
    const replayItems = new Map<string, SessionListWireItem>();
    const replayLists = new Set<string>();
    const replayAfterFetch = (query: Query) => {
      if (query.state.fetchStatus !== "idle") return;
      replayLists.delete(query.queryHash);
      applyItemsToCache(queryClient, [...replayItems.values()], getCurrentUserId());
      if (replayLists.size === 0) replayItems.clear();
    };

    // See commentsFingerprintsRef: invalidate `["comments", id]` (prefix —
    // covers the per-file variants too) when a session's fingerprint moves.
    const syncCommentsFingerprints = (items: SessionListWireItem[]) => {
      for (const item of items) {
        // `?? 0` folds the wire `null` (no comments) and an older server's
        // absent field into the same baseline shape.
        const fingerprint = `${item.comments_count ?? 0}:${item.comments_updated_at ?? 0}`;
        const previous = commentsFingerprintsRef.current.get(item.id);
        commentsFingerprintsRef.current.set(item.id, fingerprint);
        if (previous !== fingerprint) {
          void queryClient.invalidateQueries({ queryKey: ["comments", item.id] });
        }
      }
    };

    let projectsDirty = false;
    const refreshProjects = () => {
      if (!projectsDirty || queryClient.isMutating({ mutationKey: ["project-order"] }) > 0) return;
      projectsDirty = false;
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      void queryClient.invalidateQueries({ queryKey: ["project-order"] });
    };
    // Replay changes after all saves, including their settlement refetches, finish.
    const unsubscribeMutations = queryClient.getMutationCache().subscribe(refreshProjects);

    const unsubscribeFrames = sessionUpdatesSocket.subscribe((frame: SessionUpdatesFrame) => {
      switch (frame.type) {
        case "heartbeat":
          return;
        case "hosts_changed":
          void queryClient.invalidateQueries({ queryKey: ["hosts"] });
          void queryClient.invalidateQueries({ queryKey: ["session-agent"] });
          return;
        case "projects_changed":
          // Another client created/renamed/deleted a project (or changed its
          // config/icon). Only the mutating client invalidates locally, so
          // refresh the project-row caches here to converge without a reload.
          projectsDirty = true;
          refreshProjects();
          void queryClient.invalidateQueries({ queryKey: ["project-config"] });
          return;
        case "removed":
          for (const id of frame.ids) commentsFingerprintsRef.current.delete(id);
          queryClient.setQueryData<PinnedConversationsResult>(
            PINNED_CONVERSATIONS_KEY,
            (previous) =>
              previous
                ? {
                    ...previous,
                    conversations: previous.conversations.filter(
                      (row) => !frame.ids.includes(row.id),
                    ),
                  }
                : previous,
          );
          {
            const { removedAny, stranded } = removeIdsFromCache(queryClient, frame.ids);
            if (removedAny) scheduleInvalidate({ membership: true, lists: stranded });
          }
          return;
        case "snapshot":
        case "changed": {
          syncCommentsFingerprints(frame.items);
          if (frame.type === "snapshot") {
            // A snapshot restates the full watch-set, so fingerprints for
            // sessions outside it are de-watched leftovers — prune them to
            // keep the map bounded. A pruned session that re-enters the
            // watch-set re-baselines via the first-sight invalidation.
            const watchedIds = new Set(frame.items.map((item) => item.id));
            for (const id of commentsFingerprintsRef.current.keys()) {
              if (!watchedIds.has(id)) commentsFingerprintsRef.current.delete(id);
            }
          }
          // For child sessions the server includes parent_session_id in the
          // wire item. Invalidate the parent's child_sessions cache so tree
          // views (SubagentsPanel, SubagentsGraphView) pick up status changes
          // without polling.
          const parentIds = new Set<string>();
          for (const item of frame.items) {
            if (item.parent_session_id) parentIds.add(item.parent_session_id);
          }
          for (const parentId of parentIds) {
            void queryClient.invalidateQueries({ queryKey: childSessionsQueryKey(parentId) });
          }
          const { missingIds, needsRefetch, unresolvedFolders, fetchingLists } = applyItemsToCache(
            queryClient,
            frame.items,
            getCurrentUserId(),
          );
          if (fetchingLists.length > 0) {
            for (const item of frame.items) replayItems.set(item.id, item);
            for (const hash of fetchingLists) replayLists.add(hash);
          }
          // A session new to every list, or a change to a membership-deciding
          // field, can alter lists only the server can evaluate.
          //
          // Skip the active session: its updated_at bumps on open before the
          // initial list fetch returns, so it lands in missingIds even though
          // it isn't a genuinely new session. Its data is covered by
          // useSession and it's pinned in the sidebar via ActiveChatOverride.
          const sidebarMissingIds = activeIdRef.current
            ? missingIds.filter((id) => id !== activeIdRef.current)
            : missingIds;
          if (sidebarMissingIds.length > 0 || needsRefetch) {
            scheduleInvalidate({
              membership: true,
              lists: unresolvedFolders,
            });
          } else if (
            frame.type === "changed" &&
            frame.items.some((item) => item.parent_session_id == null)
          ) {
            // Search also matches message content, which any top-level change may carry.
            scheduleInvalidate({ search: true });
          }
          return;
        }
      }
    });

    // Recompute the watch-set from the cache whenever a conversations query
    // changes (initial fetch, fallback refetch, pagination, our own splices).
    let watchTimer: ReturnType<typeof setTimeout> | null = null;
    const scheduleWatch = () => {
      if (watchTimer !== null) return;
      watchTimer = setTimeout(() => {
        watchTimer = null;
        pushWatched();
      }, DEBOUNCE_MS);
    };

    pushWatched();
    const cache = queryClient.getQueryCache();
    const unsubscribeCache = cache.subscribe((event) => {
      const key = event.query.queryKey;
      // Recompute the watch-set when either the global list or a project
      // folder's list changes (fetch, pagination, splice) so newly loaded
      // folder members join the stream's watch-set.
      if (Array.isArray(key) && (key[0] === "conversations" || key[0] === "project-sessions")) {
        scheduleWatch();
        if (event.type === "updated" && replayLists.has(event.query.queryHash)) {
          replayAfterFetch(event.query);
        }
      }
      // Also recompute when a child-sessions list loads so the tree nodes
      // join the watch-set and the server starts streaming their status.
      if (Array.isArray(key) && key[0] === "conversation" && key[2] === "child_sessions") {
        scheduleWatch();
      }
    });

    return () => {
      unsubscribeFrames();
      unsubscribeMutations();
      unsubscribeCache();
      if (invalidateTimer !== null) clearTimeout(invalidateTimer);
      if (watchTimer !== null) clearTimeout(watchTimer);
      sessionUpdatesSocket.stop();
    };
  }, [identityReady, queryClient, pushWatched]);

  return children;
}
