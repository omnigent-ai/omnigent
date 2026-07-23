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
  PROJECT_LABEL_KEY,
  removeIdsFromPages,
  type ConversationsInfiniteData,
  type SessionListWireItem,
} from "@/lib/sessionListCache";
import { stopSession } from "@/lib/sessionsApi";
import {
  createProject as apiCreateProject,
  deleteProject as apiDeleteProject,
  listProjects as apiListProjects,
  renameProject as apiRenameProject,
} from "@/lib/projectsApi";
import { useChatStore } from "@/store/chatStore";
import type { Session } from "@/lib/types";
import { useSessionUpdatesConnected } from "./useSessionUpdatesConnected";
import { markConversationSeen } from "./useUnseenConversations";

export const CONNECTED_STREAM_REFETCH_INTERVAL_MS = 60_000;
export const DISCONNECTED_STREAM_REFETCH_INTERVAL_MS = 45_000;

/**
 * Query key for the archived-project-names scan (see `useArchivedProjectNames`).
 *
 * Deliberately NOT under the `["projects"]` prefix: that scan pages the whole
 * archived session list, so a shared prefix would re-run it on every
 * `invalidateQueries(["projects"])` — including project moves/deletes of
 * *non-archived* sessions that can't change the archived-project set. The
 * mutations that actually change archived membership or a project label
 * invalidate this key explicitly instead.
 */
const ARCHIVED_PROJECT_NAMES_KEY = ["archived-project-names"] as const;

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
  /**
   * Excerpt of the chat content that matched the current `search_query`,
   * centered on the match with `…` marking elided ends. Present only on
   * search responses where the query hit a message body rather than the
   * title, so the search UI (command palette) can show *where* a session
   * matched. Absent on non-search fetches and title-only matches.
   */
  search_snippet?: string | null;
  /**
   * For sub-agent sessions, the id of the direct parent session.
   * `null` / absent for top-level sessions. Included in
   * `WS /v1/sessions/updates` frames so `SessionUpdatesProvider` can
   * invalidate the parent's child-sessions cache when the child changes.
   */
  parent_session_id?: string | null;
  /**
   * First-class project this session is filed under (`projects.id`), or
   * `null`/absent when unfiled. Distinct from the legacy `omni_project`
   * label in `labels`; the sidebar groups a folder's members by EITHER this
   * id OR the label during the dual-read transition.
   */
  project_id?: string | null;
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
  project,
}: {
  after?: string;
  searchQuery: string;
  includeArchived: boolean;
  project?: string;
}): Promise<ConversationsPage> {
  // `updated_at` matches the sidebar's sort, which keeps server
  // pagination consistent with the visible order as the user scrolls.
  // See sidebarNav.ts.
  const params = new URLSearchParams({
    order: "desc",
    sort_by: "updated_at",
    limit: "30",
  });
  if (after) params.set("after", after);
  if (searchQuery) params.set("search_query", searchQuery);
  // Only request archived rows when the toggle is on, so the default
  // sidebar never pays to fetch them. The server excludes archived
  // sessions unless include_archived=true.
  if (includeArchived) params.set("include_archived", "true");
  // Scope to one project's sessions server-side. A falsy project (`undefined`
  // or `""`) is the "all projects" list, so no param is sent — matching the
  // query key (which drops `project`) and the cache-membership check. This
  // list never requests the server's "unfiled" (`project=`) slice.
  if (project) params.set("project", project);
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
 *
 * `project` optionally scopes the list to one project's sessions
 * (`?project=`, filtered server-side). It's only woven into the query key
 * when set, so the default sidebar / search callers keep their existing
 * three-element key and cache entry; a non-empty `project` produces a
 * distinct four-element key that refetches when the picker changes. Used by
 * the Archived settings view's project filter.
 */
export function useConversations(
  searchQuery: string = "",
  includeArchived: boolean = false,
  options: UseConversationsOptions = {},
  project?: string,
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
    // Keep the base three-element key for the unfiltered callers (byte-for-byte
    // unchanged, so the sidebar / rename / push-delta paths are untouched); only
    // append `project` for a concrete name. A falsy project (`undefined` or `""`)
    // is "all projects" and shares the base key — there is no distinct "" variant.
    queryKey: project
      ? ["conversations", searchQuery, includeArchived, project]
      : ["conversations", searchQuery, includeArchived],
    queryFn: ({ pageParam }) =>
      fetchConversationsPage({
        after: pageParam as string | undefined,
        searchQuery,
        includeArchived,
        project,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) =>
      lastPage.has_more ? (lastPage.last_id ?? undefined) : undefined,
    // Data is kept fresh for 30 s so components that mount in quick
    // succession after the initial fetch (AppShell, Sidebar, ChatPage)
    // share the cache instead of each triggering a background refetch.
    // The WS stream handles real-time updates within that window.
    staleTime: 30_000,
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
  // Drop any client-side queued messages for the now-deleted session; bound to
  // a dead conversation, they could never flush.
  useChatStore.getState().clearQueuedMessages(id);
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
      // Archiving can change (or empty) a project's newest member, which the
      // composer prefill reuses — refresh it so prefill never anchors on a
      // session that just left the active list.
      void queryClient.invalidateQueries({ queryKey: ["project-newest-session"] });
      // Archive membership just changed, so the archived-view picker's option
      // set may have gained/lost a project.
      void queryClient.invalidateQueries({ queryKey: ARCHIVED_PROJECT_NAMES_KEY });
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
      // The deleted session may have been a project's newest member, which
      // the composer prefill anchors on — refresh it too.
      void queryClient.invalidateQueries({ queryKey: ["project-newest-session"] });
      // Deleting an archived session may empty its project of archived members.
      void queryClient.invalidateQueries({ queryKey: ARCHIVED_PROJECT_NAMES_KEY });
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
      void queryClient.invalidateQueries({ queryKey: ["project-newest-session"] });
      void queryClient.invalidateQueries({ queryKey: ARCHIVED_PROJECT_NAMES_KEY });
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
      void queryClient.invalidateQueries({ queryKey: ["project-newest-session"] });
      void queryClient.invalidateQueries({ queryKey: ARCHIVED_PROJECT_NAMES_KEY });
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
        void queryClient.invalidateQueries({ queryKey: ["project-newest-session"] });
        void queryClient.invalidateQueries({ queryKey: ARCHIVED_PROJECT_NAMES_KEY });
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

// The reserved `conversation_labels` project key lives in the leaf cache
// module (see sessionListCache) so the cache membership checks can read it
// without a value import cycle; re-exported here for the existing consumers.
export { PROJECT_LABEL_KEY };

/**
 * A sidebar project folder. During the label→first-class transition a folder
 * is keyed by `name` (the union key that merges a first-class project and a
 * legacy label-project of the same name into one folder). `id` is the
 * first-class project id when one exists, or `null` for a label-only project
 * (which has no `projects` row yet). Operations that need an id
 * (rename/delete/file) use it, promoting label-only projects on demand.
 */
export interface ProjectSummary {
  id: string | null;
  name: string;
}

/**
 * Fetch the caller's projects from `GET /v1/sessions/projects`, which
 * dual-reads first-class projects (with id, incl. empty ones) and legacy
 * label-projects (id=null), unioned by name and sorted.
 */
export function useProjects() {
  return useQuery<ProjectSummary[]>({
    queryKey: ["projects"],
    queryFn: async () => {
      const res = await authenticatedFetch("/v1/sessions/projects");
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      return (await res.json()) as ProjectSummary[];
    },
    staleTime: 30_000,
  });
}

/**
 * Fetch the names of every project that has at least one ARCHIVED session,
 * paging through all archived sessions server-side.
 *
 * The Archived settings picker can't source options from `useProjects()`:
 * `list_projects` (GET /v1/sessions/projects) omits projects whose every
 * session is archived — exactly the population this page filters. And deriving
 * options from only the archived list's loaded first page would miss
 * archived-only projects whose sessions sit on later pages. So page through the
 * whole archived set (a larger page size keeps the request count low) and
 * collect the distinct `omni_project` labels present on archived rows.
 *
 * Exported for direct unit testing.
 */
export async function fetchAllArchivedProjectNames(): Promise<string[]> {
  const names = new Set<string>();
  let after: string | undefined;
  for (;;) {
    const params = new URLSearchParams({
      order: "desc",
      sort_by: "updated_at",
      limit: "100",
      include_archived: "true",
    });
    if (after) params.set("after", after);
    // Sequential by necessity: each page's request needs the previous page's
    // cursor (`after`), so these awaits can't be parallelized.
    // eslint-disable-next-line no-await-in-loop
    const res = await authenticatedFetch(`/v1/sessions?${params.toString()}`);
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    // eslint-disable-next-line no-await-in-loop
    const page = (await res.json()) as ConversationsPage;
    for (const conv of page.data) {
      // include_archived returns archived AND active rows; only archived ones
      // are filterable on this page, so collect labels from those.
      if (conv.archived !== true) continue;
      const name = conv.labels?.[PROJECT_LABEL_KEY];
      if (name) names.add(name);
    }
    if (!page.has_more || !page.last_id) break;
    after = page.last_id;
  }
  return [...names].sort((a, b) => a.localeCompare(b));
}

/**
 * Project names that have archived sessions — the option set for the Archived
 * view's project filter.
 *
 * Deliberately a standalone key (`ARCHIVED_PROJECT_NAMES_KEY`), NOT under the
 * `["projects"]` prefix, so the expensive full-list scan isn't dragged along
 * by unrelated `invalidateQueries(["projects"])` calls. The mutations that actually change
 * archived membership or a project label invalidate this key explicitly to keep
 * the picker in sync. Only fetched while the Archived settings view is mounted
 * (its sole caller), so the scan never runs for users who don't open it.
 */
export function useArchivedProjectNames() {
  return useQuery<string[]>({
    queryKey: ARCHIVED_PROJECT_NAMES_KEY,
    queryFn: fetchAllArchivedProjectNames,
    staleTime: 60_000,
  });
}

/**
 * Resolve a project NAME to its first-class id, creating the `projects` row on
 * demand when only a legacy label-project of that name exists (or nothing does).
 * This is how filing/renaming a label-only folder promotes it to first-class.
 *
 * Create-on-demand races: two concurrent moves to the same new name can both
 * see no existing project and both POST; the second gets a 409. Treat that as
 * benign — re-list and return the id the winner created.
 */
async function resolveOrCreateProjectId(name: string): Promise<string> {
  const projects = await apiListProjects();
  const existing = projects.find((p) => p.name === name);
  if (existing) return existing.id;
  try {
    return (await apiCreateProject(name)).id;
  } catch (err) {
    // The create may have lost a race (a concurrent move created the same
    // name → 409) or genuinely failed (500 / network). Distinguish the two by
    // re-listing: if the row now exists, a racer won — use it. Otherwise the
    // create really failed, so rethrow the ORIGINAL error rather than a generic
    // message, so the true cause (status, network) isn't masked.
    const after = await apiListProjects();
    const created = after.find((p) => p.name === name);
    if (created) return created.id;
    throw err;
  }
}

/**
 * File a session into a project (by name) or unfile it (`project === ""`), via
 * the first-class `project_id` membership on `PATCH /v1/sessions/{id}`. A
 * non-empty name is resolved to its project id (creating the row on demand for
 * a label-only folder); `""` clears membership. Exported for the new-session
 * flow, which files a freshly created session under the picked project name.
 *
 * The legacy `omni_project` label is cleared in the same PATCH: during the
 * dual-read transition the sidebar groups a folder by `project_id` OR that
 * label, so leaving a stale label would keep the session in its old
 * label-folder (and make it match two folders at once). First-class
 * `project_id` is the single source of truth after a move.
 */
export async function moveConversationToProject(
  id: string,
  project: string,
): Promise<Conversation> {
  const projectId = project === "" ? "" : await resolveOrCreateProjectId(project);
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    // "" clears membership (unfile); a non-empty id files into that project.
    // Clear the legacy label either way so the two representations don't diverge.
    body: JSON.stringify({ project_id: projectId, labels: { [PROJECT_LABEL_KEY]: "" } }),
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
      // Moving into/out of a project changes both folders' paginated lists,
      // and can change either project's newest member the prefill anchors on.
      void queryClient.invalidateQueries({ queryKey: ["project-sessions"] });
      void queryClient.invalidateQueries({ queryKey: ["project-newest-session"] });
      // Moving an archived session relabels which project owns it, shifting the
      // archived-view picker's option set.
      void queryClient.invalidateQueries({ queryKey: ARCHIVED_PROJECT_NAMES_KEY });
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
  limit = 20,
): Promise<ConversationsPage> {
  const params = new URLSearchParams({
    order: "desc",
    sort_by: "updated_at",
    limit: String(limit),
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
 * The newest (non-archived) session filed under a project, or `null` when
 * the project has no session the caller can read. Powers the new-session
 * landing screen's project prefill: starting another session in a project
 * reuses its most recent session's host, repo, and agent.
 */
export function useNewestProjectSession(project: string | null) {
  return useQuery({
    queryKey: ["project-newest-session", project],
    queryFn: async () => {
      const page = await fetchProjectSessionsPage(project as string, undefined, 1);
      return page.data[0] ?? null;
    },
    enabled: project !== null && project !== "",
    staleTime: 30_000,
  });
}

/** Archive a session AND detach it from its project (clear project_id + the
 * legacy omni_project label) in a single PATCH — used by "Delete project". */
async function archiveAndUnfileConversation(id: string): Promise<Conversation> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    // project_id="" clears first-class membership; the empty omni_project label
    // value removes the legacy label row, so the session is fully detached.
    body: JSON.stringify({ archived: true, project_id: "", labels: { [PROJECT_LABEL_KEY]: "" } }),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as Conversation;
}

/**
 * Delete a project: archive AND unfile every member session, then delete the
 * first-class `projects` row (when the folder has one). Sessions are never
 * deleted — they are detached (project_id + omni_project label cleared) and
 * archived, so they leave the sidebar but keep their history. Accepts the
 * folder's `{ id, name }`: `name` drives the member sweep (dual-read
 * `?project=`), `id` deletes the container (skipped for a label-only folder,
 * which has none). Throws `{ failed, succeeded, total }` if any member failed
 * (e.g. a shared session the user can't modify), leaving those in place.
 */
export function useDeleteProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, name }: ProjectSummary) => {
      const ids = await fetchAllProjectSessionIds(name);
      const results = await Promise.allSettled(ids.map((sid) => archiveAndUnfileConversation(sid)));
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
      // All members detached — remove the first-class container if present.
      // (A label-only folder has no row; clearing the labels above already
      // makes it vanish from the project list.)
      if (id !== null) await apiDeleteProject(id);
      return { succeeded, failed };
    },
    onSettled: () => {
      // Refresh regardless of partial failure so the sidebar reflects whatever
      // was actually archived.
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      void queryClient.invalidateQueries({ queryKey: ["project-sessions"] });
      void queryClient.invalidateQueries({ queryKey: ["project-newest-session"] });
      // Deleting a project archives its members, growing the archived set.
      void queryClient.invalidateQueries({ queryKey: ARCHIVED_PROJECT_NAMES_KEY });
    },
  });
}

/**
 * Create an empty first-class project (`POST /v1/projects`). This is the
 * capability the label model can't express — a project with no members. The
 * server rejects a duplicate name (409); the error message is surfaced to the
 * caller for inline display.
 */
export function useCreateProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => apiCreateProject(name),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
  });
}

/**
 * Rename a project. A first-class project renames its row via
 * `PATCH /v1/projects/{id}`; a label-only folder (`id === null`) is promoted on
 * demand — a row created under the new name.
 *
 * Either way, the folder's members are swept via the dual-read
 * `?project=<oldName>` and re-filed onto the target `project_id` with their
 * legacy `omni_project` label cleared. That keeps the rename coherent across
 * both membership representations during the transition: a first-class row's
 * members that were still matched by the legacy label don't get stranded in an
 * `oldName` folder, and nothing ends up matching two folders at once.
 */
export function useRenameProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      id,
      oldName,
      newName,
    }: {
      id: string | null;
      oldName: string;
      newName: string;
    }) => {
      // The target project id: rename the existing row (first-class), or
      // create one on demand (label-only folder being promoted).
      let projectId: string;
      if (id !== null) {
        await apiRenameProject(id, newName);
        projectId = id;
      } else {
        projectId = (await apiCreateProject(newName)).id;
      }
      // Reconcile the folder's members either way. During the dual-read
      // transition a member can still sit in the oldName folder via the legacy
      // omni_project label; re-file each onto project_id and clear that label so
      // the rename is coherent for both membership representations and nothing
      // is left behind in an oldName folder.
      const memberIds = await fetchAllProjectSessionIds(oldName);
      await Promise.all(
        memberIds.map(async (sid) => {
          const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sid)}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              project_id: projectId,
              labels: { [PROJECT_LABEL_KEY]: "" },
            }),
          });
          // Surface a failed re-file: a resolved-but-4xx/5xx response would
          // otherwise report success while leaving members behind.
          if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        }),
      );
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      void queryClient.invalidateQueries({ queryKey: ["project-sessions"] });
      void queryClient.invalidateQueries({ queryKey: ["project-newest-session"] });
      void queryClient.invalidateQueries({ queryKey: ARCHIVED_PROJECT_NAMES_KEY });
    },
  });
}
