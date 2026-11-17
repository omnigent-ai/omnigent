import { SidebarDataContext } from "./useSidebarData";
import {
  ARCHIVED_PROJECT_NAMES_KEY,
  type Conversation,
  PINNED_CONVERSATIONS_KEY,
  type PinnedConversationsResult,
  type ProjectSummary,
} from "./useConversations";
// App-level wiring for the `WS /v1/sessions/updates` push stream.
//
// Mounted once near the app root. It:
//   1. opens the shared session-updates WebSocket for the tab's lifetime,
//   2. derives the watch-set from whatever conversation ids are currently
//      cached across the sidebar's `["conversations", ...]` query variants,
//      and pushes it to the socket, and
//   3. applies incoming snapshot/changed/removed frames back into that cache
//      — patching field changes (status, runner, title, …) in place and
//      falling back to a paced refetch of only the lists whose membership a
//      change may have moved, which the server's list shape can't reconstruct
//      locally.
//
// This replaces the old 4 s list poll; `useConversations` keeps low-rate
// HTTP reconciliation so new sessions from other tabs / CLIs are still
// discovered while the socket handles watched-row freshness.

import { type ReactNode, useCallback, useContext, useEffect, useRef } from "react";
import { hashKey, type QueryClient, useQueryClient } from "@tanstack/react-query";
import { getCurrentUserId } from "@/lib/identity";
import { useActiveConversationId } from "@/hooks/useActiveConversationId";
import { childSessionsQueryKey, type ChildSessionInfo } from "@/hooks/useChildSessions";
import {
  isSessionArchiving,
  isSessionDeleting,
  markRecentlyCreated,
  withMoveTombstone,
} from "@/hooks/useConversations";
import {
  type ConversationsInfiniteData,
  type MembershipChange,
  type SessionListWireItem,
  collectConversationIds,
  filtersFromConversationQueryKey,
  filtersFromProjectQueryKey,
  insertNewRowsIntoPages,
  mergeItemsIntoPages,
  nullsToUndefined,
  parseProjectSessionsQueryKey,
  PROJECT_LABEL_KEY,
  removeIdsFromPages,
  settleOpenSessionRow,
  unmarkRecentlyCreated,
} from "@/lib/sessionListCache";
import { createListReconcileScheduler } from "@/lib/listReconcileScheduler";
import {
  type CachedList,
  listReconcileTargets,
  projectNamesResolver,
} from "@/lib/listReconcileTargets";
import { resolveModalHost, sessionRoutingHostId } from "@/lib/sessionHost";
import { sessionVisibility } from "@/lib/sessionVisibility";
import { type SessionUpdatesFrame, sessionUpdatesSocket } from "@/lib/sessionUpdatesSocket";
import { isTempConvId } from "@/lib/tempConversationId";

// Coalesce bursts of watch-set recomputes into one push. 250 ms is short
// enough to feel live, long enough to batch the flurry of cache writes a
// single frame can trigger.
const DEBOUNCE_MS = 250;

/** Query hash of the Archived view's project-picker scan. */
const ARCHIVED_PROJECT_NAMES_HASH = hashKey(ARCHIVED_PROJECT_NAMES_KEY);

/** Ids every page of a list holds. */
function listIds(data: ConversationsInfiniteData | undefined): Set<string> {
  return new Set(collectConversationIds([data]));
}

/**
 * Overlay wire items onto every cached `["conversations", ...]` variant.
 *
 * @param queryClient - The app QueryClient.
 * @param items - Wire items from a snapshot/changed frame.
 * @param activeId - The open session, which is never inserted when it is
 *   another user's, and never counts as a new session.
 * @param snapshotArchived - Last streamed archived flag per session, so a
 *   frozen Archived snapshot row reports its flip only once.
 * @returns Query hashes of the lists the local patch couldn't settle, which
 *   need a server refetch.
 */
function applyItemsToCache(
  queryClient: QueryClient,
  items: SessionListWireItem[],
  activeId: string | undefined,
  viewerId: string | null | undefined,
  snapshotArchived: Map<string, boolean>,
): Set<string> {
  // Frames are full rows with explicit nulls; convert null → undefined so a
  // cleared field overlays the cache in the same shape GET /v1/sessions
  // produces (absent), without tripping the permission_level === null sentinel.
  const itemsById = new Map<string, SessionListWireItem>();
  for (const item of items) {
    if (isSessionDeleting(item.id)) continue;
    itemsById.set(
      item.id,
      nullsToUndefined(
        withMoveTombstone(isSessionArchiving(item.id) ? { ...item, archived: true } : item),
      ),
    );
  }
  // The open session is always in the watch-set, so its row arrives with every
  // snapshot whether or not the server lists it. Another user's open session
  // (e.g. opened by a link shared only through a group) is never inserted: list
  // membership is the server's call, re-read on an access_changed frame or when
  // the session's permission changes.
  const activeItem = activeId === undefined ? undefined : itemsById.get(activeId);
  const skipInsert = (id: string) =>
    isSessionDeleting(id) ||
    (id === activeId &&
      activeItem !== undefined &&
      sessionVisibility(
        { owner: activeItem.owner, permission_level: activeItem.permission_level ?? null },
        viewerId ?? null,
      ) === "shared");
  const foundAnywhere = new Set<string>();
  const lists: CachedList[] = [];
  const changes = new Map<string, MembershipChange>();
  const noteChanges = (listChanges: Map<string, MembershipChange>) => {
    for (const [id, change] of listChanges) {
      const seen = changes.get(id);
      if (seen) for (const field of change.fields) seen.fields.add(field);
      else changes.set(id, { row: change.row, fields: new Set(change.fields) });
    }
  };
  // A frozen Archived snapshot row still says archived after a streamed unarchive.
  // Report that flip only when the stream's archived value changes, not on every frame.
  const noteSnapshotChanges = (listChanges: Map<string, MembershipChange>) => {
    for (const [id, change] of listChanges) {
      const fields = new Set(change.fields);
      if (fields.has("archived") && snapshotArchived.get(id) === (change.row.archived === true)) {
        fields.delete("archived");
      }
      if (fields.size > 0) noteChanges(new Map([[id, { row: change.row, fields }]]));
    }
  };
  const entries = queryClient.getQueriesData<ConversationsInfiniteData>({
    queryKey: ["conversations"],
  });
  for (const [key, data] of entries) {
    const filters = filtersFromConversationQueryKey(key);
    const snapshot =
      queryClient.getQueryCache().find({ queryKey: key, exact: true })?.meta?.snapshot === true;
    const {
      data: merged,
      found,
      needsRefetch,
      changes: listChanges,
    } = mergeItemsIntoPages(data, itemsById, filters);
    for (const id of found) foundAnywhere.add(id);
    if (snapshot) noteSnapshotChanges(listChanges);
    else noteChanges(listChanges);
    // The Archived scope is read for membership but never patched or inserted into.
    if (key[4] === "archived") {
      // A snapshot never refetches, so its frozen rows can't ask for one.
      lists.push({
        queryHash: hashKey(key),
        filters,
        ids: listIds(data),
        needsRefetch: needsRefetch && !snapshot,
      });
      continue;
    }
    // Surface a brand-new watched session (a create here or elsewhere, a share)
    // at the top now, instead of after the debounced refetch (which lags the
    // search index). An unfiled row is fully placed → mark it found so it skips
    // that refetch; a filed row can't be placed in its folder locally → leave it
    // missing so the folder still reconciles via the scheduled refetch.
    const missingHere = new Map([...itemsById].filter(([id]) => !found.has(id)));
    const { data: next, inserted } = insertNewRowsIntoPages(
      merged,
      missingHere,
      filters,
      skipInsert,
      viewerId,
    );
    for (const row of inserted) {
      if (row.project_id == null && !row.labels?.[PROJECT_LABEL_KEY]) foundAnywhere.add(row.id);
      // Keep the new row in the list-fetch until the search index catches up,
      // so the create-path refetch (or any reconcile) can't drop it before it's
      // queryable — otherwise it flashes in and out. Mirrors the delete tombstone.
      markRecentlyCreated(row);
    }
    if (next !== data) queryClient.setQueryData(key, next);
    lists.push({ queryHash: hashKey(key), filters, ids: listIds(next), needsRefetch });
  }
  // Each project folder fetches its own ["project-sessions", <name>] list, so
  // streamed field updates (pending_elicitations_count → "Needs response",
  // status, runner_online, …) must overlay those caches too — otherwise a
  // filed session's row stays frozen at fetch time. Each unsearched folder
  // cache retains its own live or archived slice.
  const projectEntries = queryClient.getQueriesData<ConversationsInfiniteData>({
    queryKey: ["project-sessions"],
  });
  for (const [key, data] of projectEntries) {
    const filters = filtersFromProjectQueryKey(key);
    const {
      data: next,
      found,
      needsRefetch,
      changes: listChanges,
    } = mergeItemsIntoPages(data, itemsById, filters);
    for (const id of found) foundAnywhere.add(id);
    noteChanges(listChanges);
    if (next !== data) queryClient.setQueryData(key, next);
    lists.push({
      queryHash: hashKey(key),
      filters,
      folder: parseProjectSessionsQueryKey(key).project,
      ids: listIds(next),
      needsRefetch,
    });
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
        // Keep optimistic standalone pins; family updates carry canonical labels.
        return {
          ...row,
          ...item,
          labels:
            row.family || item.family
              ? (item.labels ?? row.labels)
              : {
                  ...(item.labels ?? row.labels),
                  ["omnigent.pinned"]: row.labels["omnigent.pinned"],
                },
        };
      })
      .filter(
        (row) => !row.archived && (row.family?.pinned || row.labels["omnigent.pinned"] != null),
      );
    return changed ? { ...previous, conversations } : previous;
  });
  // The open session's fetched sidebar row sits outside every list, so a remote
  // change (archive, rename, an approval) reaches it only here.
  for (const item of itemsById.values()) settleOpenSessionRow(queryClient, item.id, item);

  // A watched id absent from every page is a new session whose sort position
  // can't be placed locally. Sub-agent children are watched for their tree
  // views, but no list ever holds them. The open session's updated_at bumps on
  // open before the initial list fetch returns, so it can look missing without
  // being new; useSession covers it and ActiveChatOverride keeps it listed.
  const newRows = new Map<string, Conversation>();
  for (const item of itemsById.values()) {
    if (item.parent_session_id != null || foundAnywhere.has(item.id) || item.id === activeId) {
      continue;
    }
    newRows.set(item.id, {
      object: "conversation",
      title: null,
      created_at: 0,
      updated_at: 0,
      labels: {},
      permission_level: null,
      ...item,
    });
  }
  for (const item of itemsById.values()) {
    if (item.archived !== undefined) snapshotArchived.set(item.id, item.archived === true);
  }
  const targets = listReconcileTargets({
    lists,
    changes,
    newRows,
    projectNamesOf: projectNamesResolver(queryClient.getQueryData<ProjectSummary[]>(["projects"])),
    viewerId: viewerId ?? null,
  });
  for (const { row, fields } of changes.values()) {
    // Archiving, unarchiving or refiling an archived session elsewhere can add
    // or remove a project from the Archived view's picker.
    if (fields.has("archived") || (fields.has("project") && row.archived === true)) {
      targets.add(ARCHIVED_PROJECT_NAMES_HASH);
    }
  }
  return targets;
}

/**
 * Remove ids from every cached `["conversations", ...]` variant.
 *
 * @param queryClient - The app QueryClient.
 * @param ids - Conversation ids to evict.
 * @returns Query hashes of the lists that dropped a row, which refetch to fill
 *   the gap.
 */
function removeIdsFromCache(queryClient: QueryClient, ids: string[]): Set<string> {
  const idSet = new Set(ids);
  const removedFrom = new Set<string>();
  // Both the global lists and each project folder's own list (same page shape).
  for (const queryKey of [["conversations"], ["project-sessions"]]) {
    const entries = queryClient.getQueriesData<ConversationsInfiniteData>({ queryKey });
    for (const [key, data] of entries) {
      const { data: next, removed } = removeIdsFromPages(data, idSet);
      if (removed) {
        queryClient.setQueryData(key, next);
        removedFrom.add(hashKey(key));
      }
    }
  }
  // A deleted archived session can take its project's last member with it.
  if (removedFrom.size > 0) removedFrom.add(ARCHIVED_PROJECT_NAMES_HASH);
  return removedFrom;
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

  // Last streamed archived flag per session, for the frozen Archived snapshot.
  const snapshotArchivedRef = useRef(new Map<string, boolean>());

  // Last permission level streamed for the open session, so a change to it
  // (a share upgraded or revoked) reconciles list membership.
  const activePermissionRef = useRef<{ id: string; level: number | null } | null>(null);

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
      // Only the user's own unarchived, unfiltered list decides the modal host:
      // shared, archived, search and project rows are not this user's home hosts.
      const ownLists = queryClient
        .getQueryCache()
        .getAll()
        .filter(
          (q) =>
            Array.isArray(q.queryKey) &&
            q.queryKey[0] === "conversations" &&
            q.queryKey[1] === "" &&
            q.queryKey[2] === false &&
            q.queryKey[3] == null &&
            (q.queryKey[4] == null || q.queryKey[4] === "mine") &&
            q.state.status === "success",
        );
      if (ownLists.length === 0) return false;
      // The unfiltered list also holds shared rows; only the viewer's own count.
      const viewerId = getCurrentUserId();
      const ownHostIds = ownLists.flatMap((q) => {
        const pages = (q.state.data as ConversationsInfiniteData | undefined)?.pages ?? [];
        return pages.flatMap((page) =>
          page.data
            .filter((row) => sessionVisibility(row, viewerId) === "mine")
            .map((row) => sessionRoutingHostId(row.host_id, row.labels)),
        );
      });
      resolveModalHost(ownHostIds.filter((id): id is string => id !== null));
      sessionUpdatesSocket.start();
      return true;
    };
    // Already succeeded (cache warm from a prior mount)? Start immediately.
    // Otherwise wait for the first success, then start once and unsubscribe.
    if (!tryResolveAndStart()) {
      cacheUnsub = queryClient.getQueryCache().subscribe(() => {
        if (tryResolveAndStart()) cacheUnsub?.();
      });
    }

    // Reconcile the session lists against the server when a frame shows a
    // change no local patch can place: only the lists the frame named, or
    // every list for changes the client can't attribute (a share changing, the
    // periodic round while frames starve the polls). Paced so a busy stream
    // can't stack refetch rounds against a slow server.
    const reconciler = createListReconcileScheduler((scope) =>
      scope === "all"
        ? Promise.all([
            queryClient.invalidateQueries({
              queryKey: ["conversations"],
              predicate: (query) => query.meta?.snapshot !== true,
            }),
            // Converge each project folder's own list too (new/archived/relabeled
            // members the local field-patch can't place).
            queryClient.invalidateQueries({ queryKey: ["project-sessions"] }),
            // Another client archiving/relabeling/deleting can add or remove a
            // project from the Archived view's picker; only local mutations
            // invalidate this scan otherwise.
            queryClient.invalidateQueries({ queryKey: ARCHIVED_PROJECT_NAMES_KEY }),
          ])
        : queryClient.invalidateQueries({
            predicate: (query) => scope.has(query.queryHash) && query.meta?.snapshot !== true,
          }),
    );

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
        case "access_changed":
          // The server decides whether the session now belongs in Shared.
          reconciler.schedule();
          return;
        case "removed":
          for (const id of frame.ids) {
            commentsFingerprintsRef.current.delete(id);
            snapshotArchivedRef.current.delete(id);
            // A revoked share or a deletion must not be re-injected by the
            // keep-alive once the refetch correctly leaves it out.
            unmarkRecentlyCreated(id);
          }
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
          // The open session's fetched sidebar row sits outside every list. Cancel
          // its request first so a late response can't bring the row back.
          for (const id of frame.ids) {
            const backfillKey = ["conversation-backfill", id];
            if (queryClient.getQueryState(backfillKey) === undefined) continue;
            void queryClient.cancelQueries({ queryKey: backfillKey, exact: true });
            queryClient.setQueryData<Conversation | null>(backfillKey, null);
          }
          reconciler.schedule(removeIdsFromCache(queryClient, frame.ids));
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
          const targets = applyItemsToCache(
            queryClient,
            frame.items,
            activeIdRef.current,
            getCurrentUserId(),
            snapshotArchivedRef.current,
          );
          // Reconcile list membership when the open session's permission
          // changes (e.g. its owner shares it with the viewer directly, or
          // revokes a direct share while a link share remains), not on every
          // activity update. The refetch is authoritative, so the keep-alive
          // must not re-inject the row over it.
          const active = activeIdRef.current;
          const activeItem = active ? frame.items.find((item) => item.id === active) : undefined;
          let activePermissionChanged = false;
          if (active && activeItem) {
            const level = activeItem.permission_level ?? null;
            const seen = activePermissionRef.current;
            activePermissionChanged = seen?.id === active && seen.level !== level;
            activePermissionRef.current = { id: active, level };
            if (activePermissionChanged) unmarkRecentlyCreated(active);
          }
          if (activePermissionChanged) reconciler.schedule();
          else if (targets.size > 0) reconciler.schedule(targets);
          // A targeted round doesn't count as the periodic full one.
          reconciler.noteActivity();
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
      unsubscribeMutations();
      unsubscribeCache();
      reconciler.dispose();
      if (watchTimer !== null) clearTimeout(watchTimer);
      sessionUpdatesSocket.stop();
    };
  }, [identityReady, queryClient, pushWatched]);

  return children;
}
