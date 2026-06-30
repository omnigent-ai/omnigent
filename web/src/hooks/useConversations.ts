// TanStack Query wrapper around `GET /v1/sessions`, plus mutation
// hooks for `PATCH /v1/sessions/{id}` (rename) and
// `DELETE /v1/sessions/{id}`. Rename and delete patch the cached
// lists in place (see `useRenameConversation` /
// `useStopAndDeleteConversation` — a refetch would race the server's
// async search reindex); the remaining mutations invalidate the
// conversations list on success so the sidebar reflects the change.
//
// The session-aliased routes are thin wrappers around the same store
// methods the legacy `/v1/conversations/*` routes use; wire shape is
// unchanged (still `object: "conversation"`), so the local TS types
// keep their conversation-flavored names. A future cleanup PR can
// rename `Conversation` → `Session` once all consumers move.
//
// Server returns descending-by-updated_at to match the sidebar's
// within-group sort and the per-row relative-time pill. The active
// chat's row is held in place via an in-memory override (sidebarNav
// `ActiveChatOverride`) so sends don't reorder it.

import { useMemo } from "react";
import {
  useInfiniteQuery,
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import {
  filtersFromConversationQueryKey,
  mergeItemsIntoPages,
  removeIdsFromPages,
  type ConversationsInfiniteData,
  type SessionListWireItem,
} from "@/lib/sessionListCache";
import { stopSession } from "@/lib/sessionsApi";
import type { Session } from "@/lib/types";
import { useSessionUpdatesConnected } from "./useSessionUpdatesConnected";
import { markConversationSeen } from "./useUnseenConversations";

export const CONNECTED_STREAM_REFETCH_INTERVAL_MS = 60_000;
export const DISCONNECTED_STREAM_REFETCH_INTERVAL_MS = 45_000;

export interface UseConversationsOptions {
  reconcileWhileConnected?: boolean;
}

/** Mirrors the server's `SessionListItem` / `ConversationObject` shape. */
export interface Conversation {
  id: string;
  object: "conversation";
  title: string | null;
  created_at: number;
  updated_at: number;
  labels: Record<string, string>;
  permission_level: number | null;
  owner?: string | null;
  runner_id?: string | null;
  /** Host that launched the runner for this session, e.g. ``"host_a1b2"``. */
  host_id?: string | null;
  /**
   * Absolute path the runner cd's into, e.g. ``"/Users/me/repo"``. For
   * worktree sessions this is the isolated worktree dir, not the picked
   * source repo. Powers the new-session directory-conflict hint. ``null``
   * for sessions not bound to a host workspace.
   */
  workspace?: string | null;
  /** Durable identifier of the bound agent, e.g. ``"ag_abc123"``. */
  agent_id?: string;
  /** Human-readable name of the bound agent, e.g. ``"research-agent"``. */
  agent_name?: string | null;
  /** Outstanding approval prompts — powers the sidebar "needs attention" badge. */
  pending_elicitations_count?: number;
  status?: "idle" | "running" | "failed";
  /**
   * Whether the session's runner is reachable, matching `GET /health`.
   * `GET /v1/sessions` and the `WS /v1/sessions/updates` stream include
   * it when the server has a runner-liveness lookup wired. Absent
   * (`undefined`) in focused test routers or older servers.
   */
  runner_online?: boolean | null;
  /**
   * Whether the host that launched this session's runner is reachable —
   * the host tunnel is live even if the runner itself isn't. Distinct
   * from `runner_online`, which is now strict (true only while a runner
   * tunnel is registered): a runner that died on a still-live host reads
   * `runner_online: false`, `host_online: true`. `null` when the session
   * has no `host_id` (not host-bound). Emitted alongside `runner_online`
   * by `GET /v1/sessions`, `GET /v1/sessions/{id}`, the
   * `WS /v1/sessions/updates` stream, and `GET /health`. Used only by the
   * open-session view to pick the right message when `runner_online` is
   * false (host up vs. host down); absent (`undefined`) on older servers.
   */
  host_online?: boolean | null;
  /**
   * Git branch in the session's worktree, e.g. ``"feature/login"``;
   * set only for server-created worktrees. Non-null enables the
   * "delete local branch" checkbox. See designs/SESSION_GIT_WORKTREE.md.
   */
  git_branch?: string | null;
  /**
   * Whether the session is archived. Archived sessions are hidden from
   * the sidebar's default view and surface in the "Archived" section
   * only when "Show archived" is toggled on. Defaults to false.
   */
  archived?: boolean;
  /**
   * Total review comments (any status) on this session. Together with
   * `comments_updated_at` it forms a change fingerprint: an add or edit
   * bumps the timestamp, a delete changes the count. SessionUpdatesProvider
   * invalidates the `["comments", id]` cache when either changes so the
   * CommentsPanel refreshes on external mutations. Absent on older servers.
   */
  comments_count?: number;
  /**
   * Unix **microseconds** of the most recently mutated comment on this
   * session; absent/null when it has no comments. Compared for change
   * detection only — never displayed. See `comments_count`.
   */
  comments_updated_at?: number | null;
  /**
   * The requesting user's "last seen" wall-clock baseline (seconds) for
   * this session, or null/undefined when they've never seen it. Per-viewer,
   * served by the per-user read-state cache; the sidebar's unread dot shows
   * when `updated_at > viewer_last_seen` and the session is finished. The
   * client seeds {@link useUnseenConversations}'s mirror from this on load.
   */
  viewer_last_seen?: number | null;
  /**
   * Whether the requesting user explicitly marked this session unread.
   * Per-viewer; lifts the active-row dot suppression on the client.
   */
  viewer_unread?: boolean;
}

export interface ConversationsPage {
  data: Conversation[];
  first_id: string | null;
  last_id: string | null;
  has_more: boolean;
}

/**
 * Fetch a single session as a sidebar-shaped ``Conversation`` object.
 *
 * Used to backfill pinned sessions that fall outside the paginated
 * window. Returns ``null`` on 404 (session deleted) so the caller
 * can silently drop stale pins.
 */
export async function fetchConversationById(id: string): Promise<Conversation | null> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(id)}`);
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const wire = await res.json();
  return {
    id: wire.id,
    object: "conversation",
    title: wire.title ?? null,
    created_at: wire.created_at,
    updated_at: wire.updated_at ?? wire.created_at,
    labels: wire.labels ?? {},
    permission_level: wire.permission_level ?? null,
    owner: wire.owner ?? null,
    runner_id: wire.runner_id ?? null,
    host_id: wire.host_id ?? null,
    workspace: wire.workspace ?? null,
    agent_id: wire.agent_id,
    agent_name: wire.agent_name ?? null,
    pending_elicitations_count: wire.pending_elicitations_count ?? 0,
    status: wire.status ?? "idle",
    runner_online: wire.runner_online ?? undefined,
    host_online: wire.host_online ?? undefined,
    git_branch: wire.git_branch ?? null,
    archived: wire.archived ?? false,
  };
}

async function fetchConversationsPage({
  after,
  searchQuery,
  includeArchived,
}: {
  after?: string;
  searchQuery: string;
  includeArchived: boolean;
}): Promise<ConversationsPage> {
  // `updated_at` matches the sidebar's sort, which keeps server
  // pagination consistent with the visible order as the user scrolls.
  // See sidebarNav.ts.
  const params = new URLSearchParams({
    order: "desc",
    sort_by: "updated_at",
    limit: "20",
  });
  if (after) params.set("after", after);
  if (searchQuery) params.set("search_query", searchQuery);
  // Only request archived rows when the toggle is on, so the default
  // sidebar never pays to fetch them. The server excludes archived
  // sessions unless include_archived=true.
  if (includeArchived) params.set("include_archived", "true");
  const res = await authenticatedFetch(`/v1/sessions?${params.toString()}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as ConversationsPage;
}

/**
 * Fetch the conversations list with cursor-based pagination.
 *
 * Each page holds up to 20 conversations, sorted descending by
 * `updated_at` (latest message first). `searchQuery` is forwarded to the server as
 * `?search_query=` so filtering happens server-side; callers should
 * debounce the value before passing it. `includeArchived` controls
 * whether archived sessions are fetched — it's part of the query key
 * so toggling it triggers a refetch.
 */
export function useConversations(
  searchQuery: string = "",
  includeArchived: boolean = false,
  options: UseConversationsOptions = {},
) {
  // Live updates arrive over the `WS /v1/sessions/updates` push stream
  // (SessionUpdatesProvider), which patches this cache in place as watched
  // sessions change. The stream only watches ids already present in this
  // tab's cache. Only the visible sidebar list opts into low-rate HTTP
  // reconciliation while connected, so sessions created in another tab / CLI
  // enter the sidebar without making every consumer poll `/v1/sessions`.
  // If the socket is down, all consumers use a safety poll.
  const streamConnected = useSessionUpdatesConnected();
  return useInfiniteQuery({
    queryKey: ["conversations", searchQuery, includeArchived],
    queryFn: ({ pageParam }) =>
      fetchConversationsPage({
        after: pageParam as string | undefined,
        searchQuery,
        includeArchived,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) =>
      lastPage.has_more ? (lastPage.last_id ?? undefined) : undefined,
    refetchInterval: streamConnected
      ? options.reconcileWhileConnected
        ? CONNECTED_STREAM_REFETCH_INTERVAL_MS
        : false
      : DISCONNECTED_STREAM_REFETCH_INTERVAL_MS,
  });
}

/** PATCH /v1/sessions/{id} — exported for direct unit testing. */
export async function renameConversation(id: string, title: string): Promise<Conversation> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as Conversation;
}

/**
 * Archive or unarchive a conversation via `PATCH /v1/sessions/{id}`.
 *
 * Exported for direct unit testing. `archived` is sent as the new
 * desired state, so the same helper handles both archive (`true`) and
 * unarchive (`false`).
 */
export async function archiveConversation(id: string, archived: boolean): Promise<Conversation> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ archived }),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as Conversation;
}

/**
 * DELETE /v1/sessions/{id} — exported for direct unit testing.
 *
 * `deleteBranch` opts into worktree cleanup (`?delete_branch=true`):
 * the server removes the worktree directory and deletes its branch.
 * See designs/SESSION_GIT_WORKTREE.md.
 */
export async function deleteConversation(id: string, deleteBranch = false): Promise<void> {
  const query = deleteBranch ? "?delete_branch=true" : "";
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(id)}${query}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
}

/**
 * Rename a conversation via `PATCH /v1/sessions/{id}`.
 *
 * Patches the new title into every cached list/snapshot query in
 * place instead of invalidating. `GET /v1/sessions` may serve titles
 * from a search index that catches up to the rename asynchronously
 * (the Databricks deployment lists via search-midtier over WHS), so
 * an immediate refetch races the reindex and loses — the sidebar
 * would keep the old name until the next reconciliation. The PATCH
 * response is server-confirmed truth; overlaying it is always safe.
 * List membership and ordering converge later via the
 * `WS /v1/sessions/updates` stream and the low-rate reconciliation
 * polls. Callers (the sidebar row) trigger this on blur / Enter in
 * the inline-edit input.
 */
export function useRenameConversation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) => renameConversation(id, title),
    onSuccess: (updated) => {
      // The PATCH bumps server `updated_at`, which the unseen tracker
      // would otherwise read as new messages even though the user
      // initiated this themselves. Anchor the seen-baseline to the
      // server's new updated_at so the next refetch reports not unseen.
      markConversationSeen(updated.id, updated.updated_at);
      // Overlay only the fields the rename changes — the full PATCH
      // snapshot carries nulls for absent fields that would clobber
      // list-shaped rows (see `nullsToUndefined` in sessionListCache).
      const wire: SessionListWireItem = {
        id: updated.id,
        title: updated.title,
        updated_at: updated.updated_at,
      };
      const itemsById = new Map([[updated.id, wire]]);
      for (const [key, data] of queryClient.getQueriesData<ConversationsInfiniteData>({
        queryKey: ["conversations"],
      })) {
        const { data: next } = mergeItemsIntoPages(
          data,
          itemsById,
          filtersFromConversationQueryKey(key),
          // activeId only gates `needsRefetch`, which is unused here —
          // we deliberately skip the refetch (see hook docstring).
          undefined,
        );
        if (next !== data) queryClient.setQueryData(key, next);
      }
      // The pinned-row backfill cache (staleTime 60s) and the
      // per-session snapshot (staleTime Infinity) are not covered by
      // the list patch and would serve the old title long after.
      queryClient.setQueryData<Conversation | null>(["conversation-backfill", updated.id], (old) =>
        old ? { ...old, title: updated.title, updated_at: updated.updated_at } : old,
      );
      queryClient.setQueryData<Session>(["session", updated.id], (old) =>
        old ? { ...old, title: updated.title } : old,
      );
    },
  });
}

/**
 * Archive / unarchive a conversation via `PATCH /v1/sessions/{id}`.
 *
 * Invalidates the conversations list on success so the row moves
 * into (or out of) the sidebar's "Archived" section. Mirrors the
 * rename hook's `markConversationSeen` anchoring: the PATCH bumps
 * server `updated_at`, and without this the unseen tracker would
 * flag the user's own archive action as new activity.
 */
export function useArchiveConversation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, archived }: { id: string; archived: boolean }) =>
      archiveConversation(id, archived),
    onSuccess: (updated) => {
      markConversationSeen(updated.id, updated.updated_at);
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      // Archiving/unarchiving the last (or first) non-archived member of a
      // project removes/restores it from the server's project list, and adds
      // or drops it from that project folder's own paginated list.
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      void queryClient.invalidateQueries({ queryKey: ["project-sessions"] });
    },
  });
}

/**
 * Delete a conversation: stop the running session, then
 * `DELETE /v1/sessions/{id}`.
 *
 * The DELETE route only tears down resources (env, terminals) and
 * removes the conversation row — it does NOT kill the running agent,
 * so a claude-native tmux pane or a host-spawned runner would keep
 * executing orphaned after the chat disappears from the UI. We send
 * the same `stop_session` the Stop action uses first so the live
 * process is terminated as part of the delete.
 *
 * The stop is best-effort: a failure (offline/wedged runner, an
 * already-stopped or never-running session) must not block the
 * delete, so it's swallowed and the DELETE proceeds regardless.
 *
 * Server-side, the DELETE also tears down associated tasks and tmux
 * terminals (see `routes/conversations.py:delete_conversation`).
 *
 * On success the deleted row is removed from every cached
 * `["conversations", ...]` page in place — NOT via invalidation.
 * `GET /v1/sessions` may be served from a search index that catches
 * up to the delete asynchronously (the Databricks deployment lists
 * via search-midtier over WHS), so an immediate refetch races the
 * reindex and can resurrect the just-deleted row (same race
 * `useRenameConversation` documents for titles). The per-session
 * caches are dropped too: a pinned session would otherwise re-enter
 * the sidebar from the still-fresh `["conversation-backfill", id]`
 * entry the moment it leaves the paginated pages, and stay until a
 * full reload. List pagination converges later via the
 * `WS /v1/sessions/updates` stream and the low-rate reconciliation
 * polls. Callers (the sidebar row) are responsible for navigating
 * away from `/c/{id}` if the deleted conversation is the active one.
 */
export function useStopAndDeleteConversation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, deleteBranch = false }: { id: string; deleteBranch?: boolean }) => {
      try {
        await stopSession(id);
      } catch {
        // Best-effort: proceed with delete even if the stop didn't land.
      }
      await deleteConversation(id, deleteBranch);
    },
    onSuccess: (_data, { id }) => {
      const ids = new Set([id]);
      // Drop the row from the global list AND every project folder's own
      // paginated list (["project-sessions", <name>]) — both share the same
      // page shape. Patched in place rather than invalidated for the same
      // reason as the global list: an immediate refetch races the server's
      // async search reindex and can resurrect the just-deleted row.
      for (const queryKey of [["conversations"], ["project-sessions"]]) {
        for (const [key, data] of queryClient.getQueriesData<ConversationsInfiniteData>({
          queryKey,
        })) {
          const { data: next, removed } = removeIdsFromPages(data, ids);
          if (removed) queryClient.setQueryData(key, next);
        }
      }
      queryClient.removeQueries({ queryKey: ["conversation-backfill", id] });
      queryClient.removeQueries({ queryKey: ["session", id] });
      // Deleting the last member of a project empties it, so refresh the
      // project list to drop the now-empty folder. Unlike the conversations
      // list, /v1/sessions/projects reads the DB directly (no search-index
      // lag), so this can't resurrect the deleted row.
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
  });
}

/**
 * Stop a live session via `POST /v1/sessions/{id}/events` with a
 * `stop_session` event. Unlike delete, the conversation row and its
 * transcript are kept — only the running process is terminated (for
 * claude-native sessions the bound runner hard-kills its tmux pane).
 *
 * Invalidates the conversations list on success so the sidebar's
 * session-state badge reflects the now-stopped session. The runner
 * going offline also flips `runnerOnline`, which the row badge reads.
 * Also invalidates the per-session snapshot (`["session", id]`): the
 * header merges snapshot fields over the list row (snapshot winning),
 * so a snapshot left stale at the pre-stop state would clobber the
 * now-stopped state and the header's Stop gate would lag.
 */
export function useStopSession() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => stopSession(id),
    onSuccess: (_data, id) => {
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      void queryClient.invalidateQueries({ queryKey: ["session", id] });
    },
  });
}

/**
 * Archive multiple conversations in parallel via `PATCH /v1/sessions/{id}`.
 *
 * Each session is archived independently — individual failures don't
 * block the rest. The conversations list is invalidated once on
 * completion so the sidebar refreshes. Returns an array of session IDs
 * that failed.
 */
export function useBulkArchiveConversations() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ ids, archived }: { ids: string[]; archived: boolean }) => {
      const results = await Promise.allSettled(ids.map((id) => archiveConversation(id, archived)));
      const failed: string[] = [];
      for (let i = 0; i < results.length; i++) {
        if (results[i].status === "rejected") failed.push(ids[i]);
        else
          markConversationSeen(
            ids[i],
            (results[i] as PromiseFulfilledResult<Conversation>).value.updated_at,
          );
      }
      if (failed.length > 0) throw { failed, total: ids.length };
      return results
        .filter((r): r is PromiseFulfilledResult<Conversation> => r.status === "fulfilled")
        .map((r) => r.value);
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      void queryClient.invalidateQueries({ queryKey: ["project-sessions"] });
    },
  });
}

/**
 * Delete multiple conversations in parallel (stop + delete each).
 *
 * Each session is stopped (best-effort) then deleted independently.
 * The conversations list cache is patched to remove successful
 * deletions. Returns an array of session IDs that failed.
 */
export function useBulkDeleteConversations() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (ids: string[]) => {
      const results = await Promise.allSettled(
        ids.map(async (id) => {
          try {
            await stopSession(id);
          } catch {
            // Best-effort stop
          }
          await deleteConversation(id);
        }),
      );
      const succeeded: string[] = [];
      const failed: string[] = [];
      for (let i = 0; i < results.length; i++) {
        if (results[i].status === "fulfilled") succeeded.push(ids[i]);
        else failed.push(ids[i]);
      }
      if (failed.length > 0) throw { failed, succeeded, total: ids.length };
      return { succeeded, failed };
    },
    onSuccess: (_data, ids) => {
      const idSet = new Set(ids);
      // Splice deleted rows out of the global list AND every project folder's
      // own paginated list (same page shape) so filed sessions leave their
      // folder without a refresh.
      for (const queryKey of [["conversations"], ["project-sessions"]]) {
        for (const [key, data] of queryClient.getQueriesData<ConversationsInfiniteData>({
          queryKey,
        })) {
          const { data: next, removed } = removeIdsFromPages(data, idSet);
          if (removed) queryClient.setQueryData(key, next);
        }
      }
      for (const id of ids) {
        queryClient.removeQueries({ queryKey: ["conversation-backfill", id] });
        queryClient.removeQueries({ queryKey: ["session", id] });
      }
      // Refresh the project list so a project emptied by these deletes drops
      // its now-empty folder (DB-direct read, no search-index lag).
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: (err: any) => {
      if (err?.succeeded) {
        const idSet = new Set(err.succeeded as string[]);
        for (const queryKey of [["conversations"], ["project-sessions"]]) {
          for (const [key, data] of queryClient.getQueriesData<ConversationsInfiniteData>({
            queryKey,
          })) {
            const { data: next, removed } = removeIdsFromPages(data, idSet);
            if (removed) queryClient.setQueryData(key, next);
          }
        }
        for (const id of err.succeeded) {
          queryClient.removeQueries({ queryKey: ["conversation-backfill", id] });
          queryClient.removeQueries({ queryKey: ["session", id] });
        }
        void queryClient.invalidateQueries({ queryKey: ["projects"] });
      }
    },
  });
}

/**
 * Stop multiple live sessions in parallel.
 *
 * Each session is stopped independently — individual failures don't
 * block the rest. Returns arrays of succeeded/failed IDs.
 */
export function useBulkStopSessions() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (ids: string[]) => {
      const results = await Promise.allSettled(ids.map((id) => stopSession(id)));
      const succeeded: string[] = [];
      const failed: string[] = [];
      for (let i = 0; i < results.length; i++) {
        if (results[i].status === "fulfilled") succeeded.push(ids[i]);
        else failed.push(ids[i]);
      }
      if (failed.length > 0) throw { failed, succeeded, total: ids.length };
      return { succeeded, failed };
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
}

/**
 * Fetch pinned sessions that aren't present in the loaded paginated
 * data. Returns the backfilled conversations so the caller can merge
 * them into the list before grouping.
 *
 * Each missing pinned ID fires an individual ``GET /v1/sessions/{id}``
 * via ``fetchConversationById``. Results are cached with a long stale
 * time — the low-rate list reconciliation will eventually include the
 * session once it scrolls into the loaded window, at which point the
 * individual query is no longer consulted.
 *
 * @param pinnedIds - User's pinned session IDs from localStorage.
 * @param loadedIds - Set of session IDs present in the paginated data.
 */
export function usePinnedConversationBackfill(
  pinnedIds: readonly string[],
  loadedIds: Set<string>,
): Conversation[] {
  const missingIds = pinnedIds.filter((id) => !loadedIds.has(id));
  const results = useQueries({
    queries: missingIds.map((id) => ({
      queryKey: ["conversation-backfill", id],
      queryFn: () => fetchConversationById(id),
      staleTime: 60_000,
      retry: false,
    })),
  });
  // Stabilize the returned array: only produce a new reference when
  // the set of resolved IDs actually changes. Without this, useQueries
  // returns a new array object on every render → downstream memos and
  // effects re-fire → infinite re-render loop.
  const resolvedIds = results
    .filter((r) => r.data != null)
    .map((r) => r.data!.id)
    .join(",");
  return useMemo(() => {
    const backfilled: Conversation[] = [];
    for (const result of results) {
      if (result.data) backfilled.push(result.data);
    }
    return backfilled;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resolvedIds]);
}

// ── Project hooks ─────────────────────────────────────────────────────────────

/**
 * The reserved `conversation_labels` key that stores a session's project
 * membership. Namespaced (`omni_*`) so it never collides with the user-facing
 * "project" term or other reserved keys, and is filtered out of generic label
 * surfaces.
 */
export const PROJECT_LABEL_KEY = "omni_project";

/** Fetch all project names from `GET /v1/sessions/projects`. */
export function useProjects() {
  return useQuery<string[]>({
    queryKey: ["projects"],
    queryFn: async () => {
      const res = await authenticatedFetch("/v1/sessions/projects");
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      return (await res.json()) as string[];
    },
    staleTime: 30_000,
  });
}

async function moveConversationToProject(id: string, project: string): Promise<Conversation> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    // Empty string signals "remove from project" (server deletes the label row).
    body: JSON.stringify({ labels: { [PROJECT_LABEL_KEY]: project } }),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as Conversation;
}

/**
 * Move a session to a project (or remove it from all projects when `project=""`).
 *
 * Invalidates both the conversations list (so sidebar sections re-group) and
 * the projects list (so counts update). Patch-in-place is skipped here — project
 * changes affect which sidebar section a session belongs to, so a full
 * re-render of the list is correct.
 */
export function useMoveToProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, project }: { id: string; project: string }) =>
      moveConversationToProject(id, project),
    onSuccess: (updated) => {
      markConversationSeen(updated.id, updated.updated_at);
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      // Moving into/out of a project changes both folders' paginated lists.
      void queryClient.invalidateQueries({ queryKey: ["project-sessions"] });
    },
  });
}

/**
 * Collect every session id filed under a project, paging through the
 * server-side `?project=` filter (archived included). Used by "Delete project"
 * so it removes ALL members, not just those in the loaded sidebar window.
 */
async function fetchAllProjectSessionIds(project: string): Promise<string[]> {
  const ids: string[] = [];
  let after: string | undefined;
  for (;;) {
    const params = new URLSearchParams({
      order: "desc",
      sort_by: "updated_at",
      limit: "100",
      include_archived: "true",
      project,
    });
    if (after) params.set("after", after);
    // Sequential by necessity: each page's request needs the previous page's
    // cursor (`after`), so these awaits can't be parallelized.
    // eslint-disable-next-line no-await-in-loop
    const res = await authenticatedFetch(`/v1/sessions?${params.toString()}`);
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    // eslint-disable-next-line no-await-in-loop
    const page = (await res.json()) as ConversationsPage;
    for (const conv of page.data) ids.push(conv.id);
    if (!page.has_more || !page.last_id) break;
    after = page.last_id;
  }
  return ids;
}

/**
 * Fetch up to `limit` session ids filed under a project (archived included),
 * server-side via the `?project=` filter. A single page — enough to answer
 * "is this session the project's last member?" reliably (unaffected by the
 * sidebar's loaded window or pin-precedence placement). Default `limit=2` is
 * the minimum that distinguishes "only this one" from "more than one".
 */
export async function fetchProjectSessionIds(project: string, limit = 2): Promise<string[]> {
  const params = new URLSearchParams({
    order: "desc",
    sort_by: "updated_at",
    limit: String(limit),
    include_archived: "true",
    project,
  });
  const res = await authenticatedFetch(`/v1/sessions?${params.toString()}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const page = (await res.json()) as ConversationsPage;
  return page.data.map((conv) => conv.id);
}

/** One page of a project's (non-archived) sessions, newest-first. */
async function fetchProjectSessionsPage(
  project: string,
  after?: string,
): Promise<ConversationsPage> {
  const params = new URLSearchParams({
    order: "desc",
    sort_by: "updated_at",
    limit: "20",
    project,
  });
  if (after) params.set("after", after);
  const res = await authenticatedFetch(`/v1/sessions?${params.toString()}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as ConversationsPage;
}

/**
 * Cursor-paginated list of the sessions filed under one project, fetched
 * server-side via `?project=` so a folder shows ALL its members regardless of
 * how far the global sidebar list has been scrolled. Archived sessions are
 * excluded (they leave the active sidebar). `enabled` gates the fetch so a
 * collapsed folder costs nothing — pass the folder's expanded state.
 *
 * Same page size (20) and sort (`updated_at desc`) as the global list, so a
 * folder paginates independently with its own infinite-scroll sentinel.
 */
export function useProjectSessions(project: string, enabled: boolean) {
  return useInfiniteQuery({
    queryKey: ["project-sessions", project],
    queryFn: ({ pageParam }) => fetchProjectSessionsPage(project, pageParam as string | undefined),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) =>
      lastPage.has_more ? (lastPage.last_id ?? undefined) : undefined,
    enabled,
  });
}

/**
 * Delete a whole project by ARCHIVING every session filed under it. The
 * sessions keep their `omni_project` label (so unarchiving restores them to
 * this project) and their history; they only leave the active sidebar. The
 * project is implicit and the server's project list excludes all-archived
 * projects, so the folder disappears once its last member is archived. Throws
 * `{ failed, succeeded, total }` if any session failed (e.g. a shared session
 * the user can't modify), leaving those sessions in place.
 */
export function useDeleteProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (project: string) => {
      const ids = await fetchAllProjectSessionIds(project);
      const results = await Promise.allSettled(ids.map((id) => archiveConversation(id, true)));
      const succeeded: string[] = [];
      const failed: string[] = [];
      for (let i = 0; i < results.length; i++) {
        if (results[i].status === "fulfilled") {
          succeeded.push(ids[i]);
          markConversationSeen(
            ids[i],
            (results[i] as PromiseFulfilledResult<Conversation>).value.updated_at,
          );
        } else {
          failed.push(ids[i]);
        }
      }
      if (failed.length > 0) throw { failed, succeeded, total: ids.length };
      return { succeeded, failed };
    },
    onSettled: () => {
      // Refresh regardless of partial failure so the sidebar reflects whatever
      // was actually archived.
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      void queryClient.invalidateQueries({ queryKey: ["project-sessions"] });
    },
  });
}
