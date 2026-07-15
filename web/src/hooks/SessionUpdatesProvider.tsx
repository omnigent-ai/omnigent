// App-level wiring for the `WS /v1/sessions/updates` push stream.
//
// Mounted once near the app root. It:
//   1. opens the shared session-updates WebSocket for the tab's lifetime,
//   2. derives the watch-set from whatever conversation ids are currently
//      cached across the sidebar's `["conversations", ...]` query variants,
//      and pushes it to the socket, and
//   3. applies incoming snapshot/changed/removed frames back into that cache
//      — patching field changes (status, runner, title, …) in place and
//      falling back to a debounced refetch for structural changes,
//      membership-affecting filter changes, and updated_at resorting where
//      the server's list shape can't be reconstructed locally.
//
// This replaces the old 4 s list poll; `useConversations` keeps low-rate
// HTTP reconciliation so new sessions from other tabs / CLIs are still
// discovered while the socket handles watched-row freshness.

import { type ReactNode, useCallback, useEffect, useRef } from "react";
import { type QueryClient, useQueryClient } from "@tanstack/react-query";
import { useActiveConversationId } from "@/hooks/useActiveConversationId";
import { childSessionsQueryKey, type ChildSessionInfo } from "@/hooks/useChildSessions";
import {
  type ConversationsInfiniteData,
  type SessionListWireItem,
  collectConversationIds,
  filtersFromConversationQueryKey,
  mergeItemsIntoPages,
  nullsToUndefined,
  removeIdsFromPages,
} from "@/lib/sessionListCache";
import { type SessionUpdatesFrame, sessionUpdatesSocket } from "@/lib/sessionUpdatesSocket";

// Coalesce bursts of structural changes / watch-set recomputes into one
// action. 250 ms is short enough to feel live, long enough to batch the
// flurry of cache writes a single frame can trigger.
const DEBOUNCE_MS = 250;

// A project folder's ["project-sessions", <name>] query is always the
// non-archived, unsearched slice of that project (see useProjectSessions).
// Live frames overlay those caches with these fixed filters so archived rows
// drop out the same way they do from the default sidebar list.
const PROJECT_FOLDER_FILTERS = { searchQuery: "", includeArchived: false } as const;

/**
 * Overlay wire items onto every cached `["conversations", ...]` variant.
 *
 * @param queryClient - The app QueryClient.
 * @param items - Wire items from a snapshot/changed frame.
 * @returns Ids not found in any cached page and whether any patched row
 *   needs a server refetch to preserve filtered-query membership or sort
 *   order.
 */
function applyItemsToCache(
  queryClient: QueryClient,
  items: SessionListWireItem[],
  activeId: string | undefined,
): { missingIds: string[]; needsRefetch: boolean } {
  // Frames are full rows with explicit nulls; convert null → undefined so a
  // cleared field overlays the cache in the same shape GET /v1/sessions
  // produces (absent), without tripping the permission_level === null sentinel.
  const itemsById = new Map(items.map((item) => [item.id, nullsToUndefined(item)]));
  const foundAnywhere = new Set<string>();
  let needsRefetch = false;
  const entries = queryClient.getQueriesData<ConversationsInfiniteData>({
    queryKey: ["conversations"],
  });
  for (const [key, data] of entries) {
    const {
      data: next,
      found,
      needsRefetch: queryNeedsRefetch,
    } = mergeItemsIntoPages(data, itemsById, filtersFromConversationQueryKey(key), activeId);
    for (const id of found) foundAnywhere.add(id);
    if (queryNeedsRefetch) needsRefetch = true;
    if (next !== data) queryClient.setQueryData(key, next);
  }
  // Each project folder fetches its own ["project-sessions", <name>] list, so
  // streamed field updates (pending_elicitations_count → "Needs response",
  // status, runner_online, …) must overlay those caches too — otherwise a
  // filed session's row stays frozen at fetch time. Folders are non-archived,
  // unsearched lists; an archived/label-changed row converges via the
  // debounced ["project-sessions"] invalidation the caller schedules.
  const projectEntries = queryClient.getQueriesData<ConversationsInfiniteData>({
    queryKey: ["project-sessions"],
  });
  for (const [key, data] of projectEntries) {
    const {
      data: next,
      found,
      needsRefetch: queryNeedsRefetch,
    } = mergeItemsIntoPages(data, itemsById, PROJECT_FOLDER_FILTERS, activeId);
    for (const id of found) foundAnywhere.add(id);
    if (queryNeedsRefetch) needsRefetch = true;
    if (next !== data) queryClient.setQueryData(key, next);
  }
  return {
    missingIds: [...itemsById.keys()].filter((id) => !foundAnywhere.has(id)),
    needsRefetch,
  };
}

/**
 * Remove ids from every cached `["conversations", ...]` variant.
 *
 * @param queryClient - The app QueryClient.
 * @param ids - Conversation ids to evict.
 * @returns `true` when at least one cached page changed.
 */
function removeIdsFromCache(queryClient: QueryClient, ids: string[]): boolean {
  const idSet = new Set(ids);
  let removedAny = false;
  // Both the global lists and each project folder's own list (same page shape).
  for (const queryKey of [["conversations"], ["project-sessions"]]) {
    const entries = queryClient.getQueriesData<ConversationsInfiniteData>({ queryKey });
    for (const [key, data] of entries) {
      const { data: next, removed } = removeIdsFromPages(data, idSet);
      if (removed) {
        queryClient.setQueryData(key, next);
        removedAny = true;
      }
    }
  }
  return removedAny;
}

/**
 * Open the session-updates stream and keep the conversations cache in sync
 * with it for as long as the app is mounted.
 *
 * @param children - The app subtree.
 */
export function SessionUpdatesProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();

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
    const ids = collectConversationIds([...entries, ...projectEntries].map(([, data]) => data));
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
    for (const [key, children] of childEntries) {
      // Only ["conversation", <id>, "child_sessions"] entries carry child lists.
      if (Array.isArray(key) && key[2] === "child_sessions" && Array.isArray(children)) {
        for (const child of children) {
          if (!ids.includes(child.id)) ids.push(child.id);
        }
      }
    }
    sessionUpdatesSocket.setWatched(ids);
  }, [queryClient]);

  // Navigating to an off-sidebar child changes the open session without
  // touching the conversations cache, so the cache subscription below
  // won't fire. Re-push on activeId change so the open session joins the
  // watch-set (and an already-watched id no-ops via setWatched).
  useEffect(() => {
    pushWatched();
  }, [pushWatched, activeId]);

  useEffect(() => {
    sessionUpdatesSocket.start();

    let invalidateTimer: ReturnType<typeof setTimeout> | null = null;
    const scheduleInvalidate = () => {
      if (invalidateTimer !== null) return;
      invalidateTimer = setTimeout(() => {
        invalidateTimer = null;
        void queryClient.invalidateQueries({ queryKey: ["conversations"] });
        // Converge each project folder's own list too (new/archived/relabeled
        // members the local field-patch can't place).
        void queryClient.invalidateQueries({ queryKey: ["project-sessions"] });
        // Another client archiving/relabeling/deleting can add or remove a
        // project from the Archived view's picker; only local mutations
        // invalidate this scan otherwise.
        void queryClient.invalidateQueries({ queryKey: ["archived-project-names"] });
      }, DEBOUNCE_MS);
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

    const unsubscribeFrames = sessionUpdatesSocket.subscribe((frame: SessionUpdatesFrame) => {
      switch (frame.type) {
        case "heartbeat":
          return;
        case "removed":
          for (const id of frame.ids) commentsFingerprintsRef.current.delete(id);
          if (removeIdsFromCache(queryClient, frame.ids)) scheduleInvalidate();
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
          const { missingIds, needsRefetch } = applyItemsToCache(
            queryClient,
            frame.items,
            activeIdRef.current,
          );
          // A watched id absent from every page is a new session whose sort
          // position we can't place locally. Membership-affecting deltas
          // (archive/search/connected filters) and updated_at resorting need
          // the same server-side reconciliation.
          if (missingIds.length > 0 || needsRefetch) scheduleInvalidate();
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
      }
      // Also recompute when a child-sessions list loads so the tree nodes
      // join the watch-set and the server starts streaming their status.
      if (Array.isArray(key) && key[0] === "conversation" && key[2] === "child_sessions") {
        scheduleWatch();
      }
    });

    return () => {
      unsubscribeFrames();
      unsubscribeCache();
      if (invalidateTimer !== null) clearTimeout(invalidateTimer);
      if (watchTimer !== null) clearTimeout(watchTimer);
      sessionUpdatesSocket.stop();
    };
  }, [queryClient, pushWatched]);

  return <>{children}</>;
}
