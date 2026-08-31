// TanStack Query wrapper around `GET /v1/sessions`, plus mutation
// hooks for `PATCH /v1/sessions/{id}` (rename) and
// `DELETE /v1/sessions/{id}`. Rename and delete are optimistic and patch
// the cached lists in place (see `useRenameConversation` /
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

import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import { startTimedInteraction } from "@/lib/analyticsEmit";
import {
  filtersFromConversationQueryKey,
  mergeItemsIntoPages,
  overlayTitleIntoCaches,
  PINNED_LABEL_KEY,
  PROJECT_FOLDER_FILTERS,
  PROJECT_LABEL_KEY,
  removeIdsFromPages,
  type ConversationsInfiniteData,
  type SessionListWireItem,
} from "@/lib/sessionListCache";
import { showToast } from "@/components/ui/toast";
import { revokePermission } from "@/lib/permissionsApi";
import { conversationDisplayLabel, setLegacyPinnedConversationId } from "@/shell/sidebarNav";
import { stopSession } from "@/lib/sessionsApi";
import { setSessionHost } from "@/lib/sessionHost";
import {
  createProject as apiCreateProject,
  deleteProject as apiDeleteProject,
  getProject as apiGetProject,
  listProjects as apiListProjects,
  type ProjectConfig,
  renameProject as apiRenameProject,
  updateProjectConfig as apiUpdateProjectConfig,
} from "@/lib/projectsApi";
import { releaseConversation, useChatStore } from "@/store/chatStore";
import type { Session } from "@/lib/types";
import { useSessionUpdatesConnected } from "./useSessionUpdatesConnected";
import { markConversationSeen } from "./useUnseenConversations";

export const CONNECTED_STREAM_REFETCH_INTERVAL_MS = 60_000;
export const DISCONNECTED_STREAM_REFETCH_INTERVAL_MS = 45_000;

/**
 * Client-side deadline for a *search* page fetch (`?search_query=`). Content
 * search is a substring scan server-side; if its trigram index is ever missing
 * the query can run for tens of seconds, and the palette would otherwise sit on
 * "Searching…" forever (the fetch has no timeout of its own). Aborting here
 * settles the query to a terminal state so the user sees "No results found"
 * rather than an endless spinner. Deliberately shorter than the server's own
 * search `statement_timeout` (see `_SEARCH_STATEMENT_TIMEOUT_MS` in the store)
 * so the client gives up first and the row-limited fast path still has room.
 * Only search fetches are bounded — ordinary (indexed) list pagination is left
 * untouched.
 */
export const SEARCH_FETCH_TIMEOUT_MS = 10_000;

/**
 * True for the DOMException a fetch raises when its `AbortSignal.timeout`
 * fires. Used to keep the query layer from retrying a client-side search
 * timeout: retrying would just re-arm the same slow request three more times
 * (React Query's default), turning one hung spinner into a retry storm. A real
 * server/network error still retries normally.
 */
function isAbortTimeout(error: unknown): boolean {
  return error instanceof DOMException && error.name === "TimeoutError";
}

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

export class BulkConversationMutationError extends Error {
  readonly failed: string[];
  readonly succeeded: string[];
  readonly total: number;

  constructor(
    action: string,
    { failed, succeeded = [], total }: { failed: string[]; succeeded?: string[]; total: number },
  ) {
    super(`Failed to ${action} ${failed.length} of ${total} conversations`);
    this.name = "BulkConversationMutationError";
    this.failed = failed;
    this.succeeded = succeeded;
    this.total = total;
  }
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

// ── Deleting-session tombstones ───────────────────────────────────────
//
// Delete paints optimistically: the row leaves the sidebar the moment the
// user confirms, long before the server finishes tearing the session down
// (stop, runner resources, worktree, managed sandbox — seconds). Until
// that lands, `GET /v1/sessions` still returns the session, and on the
// search-indexed deployment it keeps returning it for a while after. Any
// list fetch in that window — the reconcile poll, the refetch a WS frame
// schedules, expanding a project folder — would repaint the row the user
// just deleted.
//
// Ids recorded here are filtered out of every list fetch until the delete
// settles: dropped immediately when it fails (the row comes back), or
// after a grace window once it succeeds (the server's async reindex).
const deletingSessionIds = new Set<string>();

/** Grace window for the server's async delete reindex. */
const DELETED_TOMBSTONE_MS = 60_000;

/** Hide these sessions from every list fetch (optimistic delete). */
export function markSessionsDeleting(ids: Iterable<string>): void {
  for (const id of ids) deletingSessionIds.add(id);
}

/**
 * Stop hiding sessions — their delete failed, so the rows return.
 * Omit `ids` to release every tombstone at once.
 */
export function unmarkSessionsDeleting(ids?: Iterable<string>): void {
  if (ids === undefined) {
    deletingSessionIds.clear();
    return;
  }
  for (const id of ids) deletingSessionIds.delete(id);
}

/** Release confirmed-delete tombstones once the server has caught up. */
function expireSessionsDeleting(ids: string[]): void {
  setTimeout(() => unmarkSessionsDeleting(ids), DELETED_TOMBSTONE_MS);
}

/**
 * Drop rows for sessions with a delete in flight from a freshly fetched
 * page, recomputing the page cursors from the survivors so pagination
 * never anchors on an id the server is about to forget (mirrors
 * `removeIdsFromPages`).
 *
 * @param page - A page as returned by `GET /v1/sessions`.
 * @returns The page, or a filtered copy when it held a deleting session.
 */
function withoutDeletingSessions(page: ConversationsPage): ConversationsPage {
  if (deletingSessionIds.size === 0) return page;
  const data = page.data.filter((conv) => !deletingSessionIds.has(conv.id));
  if (data.length === page.data.length) return page;
  return {
    ...page,
    data,
    first_id: data[0]?.id ?? null,
    last_id: data[data.length - 1]?.id ?? null,
  };
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
  // Seed the session→host map on this pin-backfill path too, not just the list
  // seed / `sessionFromWire`: a pinned session outside the paginated window can
  // enter client state only through here, and terminal-attach / session-scoped
  // requests key their slice off this map — so record the host before returning
  // the row, or those requests fall back to the modal and can miss the replica.
  setSessionHost(wire.id, wire.host_id);
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
  // Bound search fetches with a client-side deadline (see
  // SEARCH_FETCH_TIMEOUT_MS): a search whose server-side index is missing can
  // hang, and the palette shows "Searching…" for the whole in-flight window.
  // Plain list pagination is indexed/fast, so it keeps no timeout.
  const signal = searchQuery ? AbortSignal.timeout(SEARCH_FETCH_TIMEOUT_MS) : undefined;
  const res = await authenticatedFetch(`/v1/sessions?${params.toString()}`, { signal });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const page = (await res.json()) as ConversationsPage;
  // Seed the session→host map from every list row, not just sessions the user
  // has opened (which is all `sessionFromWire` covers). Two payoffs: (1) the
  // modal-host pick (`resolveModalHost`, latched on this query's first settle)
  // sees ALL the user's sessions, so it picks the true most-common host rather
  // than whichever single session happened to be fetched individually first;
  // (2) a session-scoped request (`/v1/sessions/{id}/*`) issued before that
  // session's own snapshot loads still keys to the right replica instead of
  // falling back to the modal. host_id is fixed for a session's life, so this
  // can't seed a stale value; a hostless row clears any prior mapping.
  for (const row of page.data) setSessionHost(row.id, row.host_id);
  return withoutDeletingSessions(page);
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
// Latch so the list_sessions CUJ times only the first full-list fetch per app
// load. `useConversations` is observed by several components sharing one query
// cache, and it also refetches on a background poll / WS reconcile / pagination —
// none of which are the user "open the list" action. A module-scoped flag times
// exactly one fetch regardless of observer count, then stays latched.
let initialListLoadTimed = false;

export function useConversations(
  searchQuery = "",
  includeArchived = false,
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
    queryFn: async ({ pageParam }) => {
      const fetchPage = () =>
        fetchConversationsPage({
          after: pageParam as string | undefined,
          searchQuery,
          includeArchived,
          project,
        });
      // Time the first full-list load per app session; skip pagination
      // (pageParam set) and every later fetch (poll / reconcile / invalidation).
      if (initialListLoadTimed || pageParam !== undefined) return fetchPage();
      initialListLoadTimed = true;
      const interaction = startTimedInteraction("list_sessions");
      try {
        const page = await fetchPage();
        interaction.complete();
        return page;
      } catch (error) {
        interaction.fail();
        throw error;
      }
    },

    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) =>
      lastPage.has_more ? (lastPage.last_id ?? undefined) : undefined,
    // Data is kept fresh for 30 s so components that mount in quick
    // succession after the initial fetch (AppShell, Sidebar, ChatPage)
    // share the cache instead of each triggering a background refetch.
    // The WS stream handles real-time updates within that window.
    staleTime: 30_000,
    // A client-side search timeout is terminal — retrying would just re-arm the
    // same slow request (default 3×) and prolong the spinner. Any other failure
    // keeps React Query's default retry behaviour.
    retry: (failureCount, error) => !isAbortTimeout(error) && failureCount < 3,
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
  // A deleted conversation must stop pumping and let go of its stream.
  releaseConversation(id);
}

/**
 * Rename a conversation via `PATCH /v1/sessions/{id}`.
 *
 * Optimistic: the new title is overlaid into every cached list/snapshot
 * query in `onMutate`, so the sidebar row repaints on the next frame
 * rather than after the PATCH round-trips (which showed the old name
 * for the request's duration). `onSuccess` re-overlays the
 * server-confirmed row to pick up the authoritative `updated_at`;
 * `onError` restores the pre-rename title.
 *
 * Overlaying in place instead of invalidating is deliberate:
 * `GET /v1/sessions` may serve titles from a search index that catches
 * up to the rename asynchronously (the Databricks deployment lists via
 * search-midtier over WHS), so an immediate refetch races the reindex
 * and loses — the sidebar would keep the old name until the next
 * reconciliation. List membership and ordering converge later via the
 * `WS /v1/sessions/updates` stream and the low-rate reconciliation
 * polls. Callers (the sidebar row) trigger this on blur / Enter in
 * the inline-edit input.
 */
export function useRenameConversation() {
  const queryClient = useQueryClient();

  // Overlay a title into every cache holding a row for this session. Shared
  // with the `session.title` SSE handler so a terminal-side `/rename` and a
  // Web UI rename patch exactly the same caches.
  const overlayTitle = (id: string, title: string | null, updatedAt?: number) =>
    overlayTitleIntoCaches(queryClient, id, title, updatedAt);

  return useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) => renameConversation(id, title),
    // Paint the new name immediately; snapshot the current title so a
    // failed PATCH can roll back to whatever the caches held before. The
    // sidebar reads from the ["conversations"] lists, so prefer that row's
    // title and fall back to the snapshot / backfill caches.
    onMutate: async ({ id, title }) => {
      // Cancel any in-flight list refetch (the reconcile poll or a WS-triggered
      // fetch) so it can't resolve after this overlay and clobber the new title
      // with the stale search-indexed name.
      await Promise.all([
        queryClient.cancelQueries({ queryKey: ["conversations"] }),
        queryClient.cancelQueries({ queryKey: ["project-sessions"] }),
      ]);
      // Find the row's current title in whichever list cache holds it — the
      // flat sidebar list or a project folder — so a failed PATCH can revert.
      let listTitle: string | null | undefined;
      for (const queryKey of [["conversations"], ["project-sessions"]]) {
        for (const [, data] of queryClient.getQueriesData<ConversationsInfiniteData>({
          queryKey,
        })) {
          const row = data?.pages.flatMap((page) => page.data).find((conv) => conv.id === id);
          if (row) {
            listTitle = row.title;
            break;
          }
        }
        if (listTitle !== undefined) break;
      }
      const previous = {
        list: listTitle,
        session: queryClient.getQueryData<Session>(["session", id])?.title,
        backfill: queryClient.getQueryData<Conversation | null>(["conversation-backfill", id])
          ?.title,
      };
      overlayTitle(id, title);
      return previous;
    },
    onError: (_err, { id }, previous) => {
      // Restore the first title we managed to snapshot. A snapshotted null
      // (previously-untitled row) is a real value to restore; only skip
      // when we never captured a title at all (all sources undefined).
      const restored =
        previous?.list !== undefined
          ? previous.list
          : previous?.session !== undefined
            ? previous.session
            : previous?.backfill;
      if (restored !== undefined) overlayTitle(id, restored);
    },
    onSuccess: (updated) => {
      // The PATCH bumps server `updated_at`, which the unseen tracker
      // would otherwise read as new messages even though the user
      // initiated this themselves. Anchor the seen-baseline to the
      // server's new updated_at so the next refetch reports not unseen.
      markConversationSeen(updated.id, updated.updated_at);
      // Reconcile with the server-confirmed title + authoritative updated_at.
      overlayTitle(updated.id, updated.title, updated.updated_at);
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
      // Archive membership just changed, so the archived-view picker's option
      // set may have gained/lost a project.
      void queryClient.invalidateQueries({ queryKey: ARCHIVED_PROJECT_NAMES_KEY });
    },
  });
}

/**
 * Splice conversations out of every cached sidebar list at once.
 *
 * Covers the global `["conversations", ...]` variants, each project
 * folder's own `["project-sessions", <name>]` list (same page shape), and
 * the sibling Pinned cache the two sweeps above don't reach — a deleted
 * pinned row would otherwise linger there until a reload.
 *
 * Patched in place rather than invalidated: `GET /v1/sessions` may be
 * served from a search index that catches up to the delete asynchronously
 * (the Databricks deployment lists via search-midtier over WHS), so a
 * refetch races the reindex and can resurrect the row (the same race
 * `useRenameConversation` documents for titles).
 *
 * @param queryClient - The app QueryClient.
 * @param ids - Conversation ids to drop from the lists.
 */
function removeConversationsFromLists(queryClient: QueryClient, ids: Set<string>): void {
  for (const queryKey of [["conversations"], ["project-sessions"]]) {
    for (const [key, data] of queryClient.getQueriesData<ConversationsInfiniteData>({
      queryKey,
    })) {
      const { data: next, removed } = removeIdsFromPages(data, ids);
      if (removed) queryClient.setQueryData(key, next);
    }
  }
  queryClient.setQueryData<PinnedConversationsResult>(PINNED_CONVERSATIONS_KEY, (old) =>
    old ? { ...old, conversations: old.conversations.filter((c) => !ids.has(c.id)) } : old,
  );
}

/** Every cached list touched by an optimistic delete, as it was before. */
interface DeletedListsSnapshot {
  lists: [readonly unknown[], ConversationsInfiniteData | undefined][];
  pinned: PinnedConversationsResult | undefined;
}

/**
 * Paint a delete before the server confirms it: hide the sessions from
 * every list fetch, then splice their rows out of the cached lists.
 *
 * In-flight list refetches are cancelled first so one can't resolve after
 * the splice and repaint the rows the user just deleted.
 *
 * @param queryClient - The app QueryClient.
 * @param ids - Conversation ids being deleted.
 * @returns The pre-delete cache state, for `restoreDeletedConversations`.
 */
async function paintConversationsDeleted(
  queryClient: QueryClient,
  ids: readonly string[],
): Promise<DeletedListsSnapshot> {
  markSessionsDeleting(ids);
  await Promise.all([
    queryClient.cancelQueries({ queryKey: ["conversations"] }),
    queryClient.cancelQueries({ queryKey: ["project-sessions"] }),
  ]);
  const snapshot: DeletedListsSnapshot = {
    lists: [
      ...queryClient.getQueriesData<ConversationsInfiniteData>({ queryKey: ["conversations"] }),
      ...queryClient.getQueriesData<ConversationsInfiniteData>({ queryKey: ["project-sessions"] }),
    ],
    pinned: queryClient.getQueryData<PinnedConversationsResult>(PINNED_CONVERSATIONS_KEY),
  };
  removeConversationsFromLists(queryClient, new Set(ids));
  return snapshot;
}

/**
 * Put optimistically-removed rows back after a failed delete.
 *
 * Restores the snapshot wholesale (the same rollback shape
 * `useMoveToProject` uses) rather than re-deriving positions, then
 * re-splices whatever DID delete — a bulk delete can fail for only part
 * of its selection. The lists are invalidated afterwards so the server
 * reconciles the window the snapshot froze over.
 *
 * @param queryClient - The app QueryClient.
 * @param snapshot - Cache state captured by `paintConversationsDeleted`.
 * @param failedIds - Conversation ids whose delete failed (rows return).
 * @param deletedIds - Conversation ids that did delete (rows stay gone).
 */
function restoreDeletedConversations(
  queryClient: QueryClient,
  snapshot: DeletedListsSnapshot | undefined,
  failedIds: readonly string[],
  deletedIds: readonly string[] = [],
): void {
  unmarkSessionsDeleting(failedIds);
  if (snapshot) {
    for (const [key, data] of snapshot.lists) queryClient.setQueryData(key, data);
    queryClient.setQueryData(PINNED_CONVERSATIONS_KEY, snapshot.pinned);
    if (deletedIds.length > 0) removeConversationsFromLists(queryClient, new Set(deletedIds));
  }
  void queryClient.invalidateQueries({ queryKey: ["conversations"] });
  void queryClient.invalidateQueries({ queryKey: ["project-sessions"] });
}

/**
 * Drop the caches that only a confirmed delete makes dead: the
 * per-session snapshot and the pinned-row backfill entry (a pinned
 * session would otherwise re-enter the sidebar from the still-fresh
 * `["conversation-backfill", id]` entry the moment it leaves the
 * paginated pages, and stay until a full reload).
 *
 * @param queryClient - The app QueryClient.
 * @param ids - Conversation ids the server confirmed deleted.
 */
function finalizeDeletedConversations(queryClient: QueryClient, ids: readonly string[]): void {
  for (const id of ids) {
    queryClient.removeQueries({ queryKey: ["conversation-backfill", id] });
    queryClient.removeQueries({ queryKey: ["session", id] });
  }
  expireSessionsDeleting([...ids]);
  // Deleting the last member of a project empties it, so refresh the
  // project list to drop the now-empty folder. Unlike the conversations
  // list, /v1/sessions/projects reads the DB directly (no search-index
  // lag), so this can't resurrect the deleted rows.
  void queryClient.invalidateQueries({ queryKey: ["projects"] });
  // Deleting an archived session may empty its project of archived members.
  void queryClient.invalidateQueries({ queryKey: ARCHIVED_PROJECT_NAMES_KEY });
}

/**
 * Delete a conversation: stop the running session, then
 * `DELETE /v1/sessions/{id}`.
 *
 * The DELETE route tears down resources (env, terminals) and removes the
 * conversation row, but only the `stop_session` event tears down a
 * host-spawned session's dedicated runner — without it that runner stays
 * connected after the chat is gone. We send the same `stop_session` the
 * Stop action uses first so the live process is terminated as part of the
 * delete.
 *
 * The stop is best-effort: a failure (offline/wedged runner, an
 * already-stopped or never-running session) must not block the
 * delete, so it's swallowed and the DELETE proceeds regardless.
 *
 * Optimistic: the row leaves every cached list in `onMutate`, so the
 * sidebar repaints on the next frame instead of after the stop + DELETE
 * round-trip — server-side teardown (runner resources, worktree, managed
 * sandbox) can take seconds, which is what made delete feel slower than
 * archive. The session is tombstoned for that window so a concurrent list
 * fetch can't repaint the row (see `withoutDeletingSessions`); `onError`
 * drops the tombstone and refetches, restoring the row. Callers (the
 * sidebar row) are responsible for navigating away from `/c/{id}` if the
 * deleted conversation is the active one.
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
    onMutate: async ({ id }) => {
      // Snapshot the label before the row leaves the cache — a failure
      // toast fired later can no longer look it up.
      const row = findCachedConversationRow(queryClient, id);
      const snapshot = await paintConversationsDeleted(queryClient, [id]);
      return { label: row ? conversationDisplayLabel(row) : null, snapshot };
    },
    onError: (_err, { id }, context) => {
      restoreDeletedConversations(queryClient, context?.snapshot, [id]);
      // The row is back in the sidebar but nothing else marks it as failed
      // (the row unmounted when it was spliced out, taking any in-row error
      // state with it), so the toast is the only failure signal.
      showToast(
        context?.label
          ? `Couldn't delete ${context.label} — it's back in the sidebar.`
          : "Couldn't delete the session — it's back in the sidebar.",
      );
    },
    onSuccess: (_data, { id }) => {
      finalizeDeletedConversations(queryClient, [id]);
    },
  });
}

/**
 * Leave a session shared with you — revoking your OWN grant via
 * `DELETE /v1/sessions/{id}/permissions/{viewerId}` — so it drops out of the
 * sidebar. The server allows a self-revoke with only read access; `viewerId`
 * must be the caller's own id (the sidebar reads it from the resolved identity
 * it already uses to decide the row isn't owned).
 *
 * The row is spliced out of the cached list pages in place rather than
 * invalidated, for the same reason delete does it (see
 * `useStopAndDeleteConversation`): `GET /v1/sessions` may be served from a
 * search index that catches up asynchronously, so an immediate refetch can
 * resurrect the row the user just dismissed. Nothing is deleted server-side —
 * only the caller's grant — so the owner re-sharing brings it back.
 *
 * Callers are responsible for navigating away from `/c/{id}` when leaving the
 * session currently being viewed; it 404s for them afterwards.
 */
export function useLeaveSession() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, viewerId }: { id: string; viewerId: string }) =>
      revokePermission(id, viewerId),
    onSuccess: (_data, { id }) => {
      const ids = new Set([id]);
      for (const queryKey of [["conversations"], ["project-sessions"]]) {
        for (const [key, data] of queryClient.getQueriesData<ConversationsInfiniteData>({
          queryKey,
        })) {
          const { data: next, removed } = removeIdsFromPages(data, ids);
          if (removed) queryClient.setQueryData(key, next);
        }
      }
      // Drop the per-session caches too, so the row can't re-enter the sidebar
      // from a still-fresh backfill entry once it leaves the paginated pages.
      queryClient.removeQueries({ queryKey: ["conversation-backfill", id] });
      queryClient.removeQueries({ queryKey: ["session", id] });
      queryClient.setQueryData<PinnedConversationsResult>(PINNED_CONVERSATIONS_KEY, (old) =>
        old ? { ...old, conversations: old.conversations.filter((c) => !ids.has(c.id)) } : old,
      );
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
      if (failed.length > 0) {
        throw new BulkConversationMutationError("archive", { failed, total: ids.length });
      }
      return results
        .filter((r): r is PromiseFulfilledResult<Conversation> => r.status === "fulfilled")
        .map((r) => r.value);
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      void queryClient.invalidateQueries({ queryKey: ["project-sessions"] });
      void queryClient.invalidateQueries({ queryKey: ARCHIVED_PROJECT_NAMES_KEY });
    },
  });
}

/**
 * Delete multiple conversations in parallel (stop + delete each).
 *
 * Each session is stopped (best-effort) then deleted independently.
 * Optimistic like the single-session delete: every selected row leaves
 * the cached lists in `onMutate` so the sidebar repaints immediately,
 * and only the ones whose delete failed are restored. Returns the
 * succeeded / failed session IDs.
 *
 * `deleteBranchIds` opts individual worktree sessions into git cleanup:
 * a session whose id is in the set is deleted with `?delete_branch=true`
 * (the server removes its worktree and branch). Ids absent from the set
 * — or all ids when it's omitted — delete without touching any branch.
 */
export function useBulkDeleteConversations() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      ids,
      deleteBranchIds,
    }: {
      ids: string[];
      deleteBranchIds?: ReadonlySet<string>;
    }) => {
      const results = await Promise.allSettled(
        ids.map(async (id) => {
          try {
            await stopSession(id);
          } catch {
            // Best-effort stop
          }
          await deleteConversation(id, deleteBranchIds?.has(id) ?? false);
        }),
      );
      const succeeded: string[] = [];
      const failed: string[] = [];
      for (let i = 0; i < results.length; i++) {
        if (results[i].status === "fulfilled") succeeded.push(ids[i]);
        else failed.push(ids[i]);
      }
      if (failed.length > 0) {
        throw new BulkConversationMutationError("delete", {
          failed,
          succeeded,
          total: ids.length,
        });
      }
      return { succeeded, failed };
    },
    onMutate: ({ ids }) => paintConversationsDeleted(queryClient, ids),
    onSuccess: (_data, { ids }) => {
      finalizeDeletedConversations(queryClient, ids);
    },
    onError: (error, { ids }, snapshot) => {
      // Partial failure: the sessions that did delete stay gone; the rest
      // come back. A non-bulk error (nothing reported) restores everything.
      const bulk = error instanceof BulkConversationMutationError ? error : null;
      const failed = bulk ? bulk.failed : [...ids];
      const succeeded = bulk ? bulk.succeeded : [];
      restoreDeletedConversations(queryClient, snapshot, failed, succeeded);
      if (succeeded.length > 0) finalizeDeletedConversations(queryClient, succeeded);
      showToast(
        failed.length === 1
          ? "Couldn't delete 1 session — it's back in the sidebar."
          : `Couldn't delete ${failed.length} sessions — they're back in the sidebar.`,
      );
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
      if (failed.length > 0) {
        throw new BulkConversationMutationError("stop", {
          failed,
          succeeded,
          total: ids.length,
        });
      }
      return { succeeded, failed };
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
  });
}

/**
 * Move multiple sessions to a project (or remove from all projects when
 * `project=""`). Each session is moved independently; partial failures don't
 * block the rest.
 */
export function useBulkMoveToProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ ids, project }: { ids: string[]; project: string }) => {
      const results = await Promise.allSettled(
        ids.map((id) => moveConversationToProject(id, project)),
      );
      const failed: string[] = [];
      for (let i = 0; i < results.length; i++) {
        if (results[i].status === "rejected") failed.push(ids[i]);
      }
      if (failed.length > 0) {
        throw new BulkConversationMutationError("move", { failed, total: ids.length });
      }
      return results
        .filter((r): r is PromiseFulfilledResult<Conversation> => r.status === "fulfilled")
        .map((r) => r.value);
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      void queryClient.invalidateQueries({ queryKey: ["project-sessions"] });
      void queryClient.invalidateQueries({ queryKey: ARCHIVED_PROJECT_NAMES_KEY });
    },
  });
}

// ── Pinned hooks ──────────────────────────────────────────────────────────────

// The reserved `conversation_labels` pinned key lives in the leaf cache module
// (see sessionListCache) so membership checks can read it without a value
// import cycle; re-exported here for the existing consumers.
export { PINNED_LABEL_KEY };

// TanStack Query key for the server-authoritative pinned-session list.
// Deliberately NOT under the ["conversations"] prefix: the list mutations do
// prefix-matched `getQueriesData(["conversations"])` sweeps (and
// `filtersFromConversationQueryKey`, which throws on a non-list key) plus
// `invalidateQueries(["conversations"])`. A pinned key nested under that prefix
// would be swept into those loops — the throw aborted the toggle's cache patch,
// so a pinned row didn't move until a refetch. A sibling key keeps it isolated.
export const PINNED_CONVERSATIONS_KEY = ["pinned-conversations"] as const;

/**
 * Result of the pinned-session query. `conversations` is the viewer's pinned
 * sessions; `filterHonored` says whether the server actually applied the
 * `?pinned=true` filter (see {@link fetchPinnedConversations}).
 */
export interface PinnedConversationsResult {
  conversations: Conversation[];
  /**
   * False when the server ignored the `pinned` query param — i.e. a
   * pre-upgrade server that predates server-side pins. Such a server returns
   * an ordinary (unfiltered) session page whose rows carry no `omnigent.pinned`
   * label. The sidebar reads this to keep the one-time localStorage→server pin
   * migration inert until the server can actually store pins, so a
   * UI-before-server upgrade can't wipe local pins.
   */
  filterHonored: boolean;
}

/**
 * Fetch every pinned session (the `omnigent.pinned` label) via
 * `GET /v1/sessions?pinned=true`, independent of the sidebar's paginated
 * window. Pins now live on the server (a session label) so they follow the
 * user across devices; this query is the source of truth for which sessions
 * are pinned and supplies the rows for any pin that sits outside the loaded
 * list. A generous single page (100) covers realistic pin counts.
 *
 * A pre-upgrade server doesn't know the `pinned` param and silently ignores
 * it (FastAPI drops unknown query params), returning an ordinary session page.
 * We defend against that by keeping only rows that actually carry the
 * `omnigent.pinned` label and reporting `filterHonored: false` whenever the
 * server handed back rows that aren't pinned — the signal the sidebar uses to
 * skip the destructive localStorage migration against an old server.
 */
export async function fetchPinnedConversations(): Promise<PinnedConversationsResult> {
  const params = new URLSearchParams({
    order: "desc",
    sort_by: "updated_at",
    limit: "100",
    pinned: "true",
  });
  const res = await authenticatedFetch(`/v1/sessions?${params.toString()}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const rows = withoutDeletingSessions((await res.json()) as ConversationsPage).data;
  const conversations = rows.filter((c) => c.labels?.[PINNED_LABEL_KEY] != null);
  // Honored iff the server returned nothing, or everything it returned is
  // actually pinned. An old server returns unfiltered rows (none pinned), so a
  // non-empty raw page with fewer pinned rows means the filter was ignored.
  //
  // Ambiguity: an EMPTY page is treated as honored, but "new server, no pins"
  // and "old server, zero sessions" are indistinguishable here — we infer
  // honored-ness from row contents rather than an explicit capability marker.
  // This is safe in practice: an old server returns an empty page only when the
  // account has no sessions at all, so any leftover localStorage pin ids point
  // at sessions that no longer exist. The migration's PATCH to those ids 404s,
  // the write is recorded as failed, and the id is RETAINED in localStorage
  // (not cleared) — so no pin is lost even in this corner. Closing the window
  // fully would need a server capability flag; deferred since pins haven't
  // shipped yet.
  const filterHonored = rows.length === 0 || conversations.length === rows.length;
  return { conversations, filterHonored };
}

/** Server-authoritative list of the viewer's pinned sessions. */
export function usePinnedConversations() {
  return useQuery<PinnedConversationsResult>({
    queryKey: PINNED_CONVERSATIONS_KEY,
    queryFn: fetchPinnedConversations,
    staleTime: 30_000,
  });
}

/**
 * PATCH the pinned label on a session. Pinning stores the epoch-ms pin time as
 * the value (so the Pinned section can order by pin recency); an empty string
 * signals unpin — the server deletes the label row rather than persisting an
 * empty value (labels are upsert-only). An optional `pinnedAt` lets the
 * localStorage migration preserve each legacy pin's relative order.
 */
export async function setConversationPinned(
  id: string,
  pinned: boolean,
  pinnedAt: number = Date.now(),
): Promise<Conversation> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      labels: { [PINNED_LABEL_KEY]: pinned ? String(pinnedAt) : "" },
    }),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as Conversation;
}

/**
 * Locate the fullest cached copy of a session row across the sidebar's list
 * caches: the pinned section, every `["conversations", ...]` variant, and the
 * `["project-sessions", name]` folder lists (a filed session outside the main
 * window lives only in its folder's cache). Used by the pin and move overlays
 * to read the row's current fields before patching them optimistically.
 */
function findCachedConversationRow(queryClient: QueryClient, id: string): Conversation | undefined {
  return (
    queryClient
      .getQueryData<PinnedConversationsResult>(PINNED_CONVERSATIONS_KEY)
      ?.conversations.find((c) => c.id === id) ??
    queryClient
      .getQueriesData<ConversationsInfiniteData>({ queryKey: ["conversations"] })
      .flatMap(([, data]) => data?.pages.flatMap((p) => p.data) ?? [])
      .find((c) => c.id === id) ??
    queryClient
      .getQueriesData<ConversationsInfiniteData>({ queryKey: ["project-sessions"] })
      .flatMap(([, data]) => data?.pages.flatMap((p) => p.data) ?? [])
      .find((c) => c.id === id)
  );
}

/**
 * Pin / unpin a session via `PATCH /v1/sessions/{id}` (the `omnigent.pinned`
 * label). Overlays only the `labels` field onto cached rows and patches the
 * pinned-list query directly (adding the row on pin, removing it on unpin)
 * rather than invalidating it. `GET /v1/sessions?pinned=true` is served from a
 * label index that catches up to the write asynchronously (same reindex lag the
 * rename / delete hooks document), so an immediate refetch races it and would
 * return the pre-toggle set — the Pinned section would flip back until a manual
 * refresh. The query's `staleTime` converges it later.
 *
 * The PATCH returns a `SessionResponse` snapshot, which carries `labels` but
 * NOT `updated_at` (only the list endpoint's `SessionListItem` has it). So the
 * row inserted into the pinned cache is built from the existing cached row (for
 * its `updated_at`, `title`, etc.) with the server-confirmed `labels` overlaid
 * — never from the raw PATCH response, which would leave the row without a
 * timestamp until the pinned query refetched (a blank time field on the new
 * row). For the same reason the list overlay carries only `labels`, so pinning
 * can't blank an existing row's timestamp.
 *
 * Old-server fallback: when the server can't store pins (`filterHonored` is
 * false — a pre-upgrade server that ignores `?pinned=true`), a PATCH would
 * persist a bare `omnigent.pinned` key that the upgraded server discards on
 * read (it only surfaces the caller's per-user `omnigent.pinned.<user>` key),
 * so the pin would silently vanish on the server upgrade. Instead we write the
 * pin to localStorage — the same store the pre-upgrade UI used — so it survives
 * and later migrates through `useMigrateLocalPinsToServer` like any other
 * pre-upgrade pin. The optimistic cache patch still runs, and the sidebar
 * unions localStorage pins into the Pinned section, so it shows immediately.
 */
export function useTogglePinnedConversation() {
  const queryClient = useQueryClient();

  // Whether the server can store pins. Sourced from the pinned query's
  // `filterHonored`; defaults to true when the query hasn't loaded (a manual
  // toggle implies a reachable, working server — the same assumption the cache
  // patch below makes).
  const serverCanStorePins = (): boolean =>
    queryClient.getQueryData<PinnedConversationsResult>(PINNED_CONVERSATIONS_KEY)?.filterHonored ??
    true;

  // Locate the fullest cached row for an id, so the pinned insert keeps a real
  // `updated_at` (the PATCH snapshot lacks it).
  const findRow = (id: string): Conversation | undefined =>
    findCachedConversationRow(queryClient, id);

  // Apply a pin/unpin to every cache that renders the row. `labels` is the
  // authoritative label map to write; membership in the Pinned section is
  // driven by the PINNED_CONVERSATIONS_KEY cache, so that patch is what makes
  // the row visibly move.
  const patch = (id: string, labels: Record<string, string>, pinned: boolean) => {
    const existing = findRow(id);
    // The pin toggle only changes `labels`; overlay just that so it can't
    // clobber other fields (e.g. blank `updated_at`) on the list rows.
    const itemsById = new Map([[id, { id, labels } satisfies SessionListWireItem]]);
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
    queryClient.setQueryData<Conversation | null>(["conversation-backfill", id], (old) =>
      old ? { ...old, labels } : old,
    );
    queryClient.setQueryData<Session>(["session", id], (old) => (old ? { ...old, labels } : old));
    // Add the row on pin (its label carries the pin timestamp the sidebar sorts
    // by) built from the existing row + labels; drop it on unpin.
    queryClient.setQueryData<PinnedConversationsResult>(PINNED_CONVERSATIONS_KEY, (old) => {
      // Preserve the query's `filterHonored` flag; the toggle only mutates the
      // list. If the query hasn't loaded yet, an optimistic patch implies the
      // server can store pins, so treat it as honored.
      const prev = old ?? { conversations: [], filterHonored: true };
      const rest = prev.conversations.filter((c) => c.id !== id);
      if (!pinned) return { ...prev, conversations: rest };
      // Prefer the full cached row (keeps title/updated_at); fall back to a
      // minimal row when the session isn't in any loaded cache (rare — the pin
      // affordance lives on a visible row). The pinned query refetch fills the
      // rest in later.
      const row: Conversation = existing
        ? { ...existing, labels }
        : ({ id, object: "conversation", labels } as Conversation);
      return { ...prev, conversations: [...rest, row] };
    });
  };

  return useMutation({
    mutationFn: ({ id, pinned }: { id: string; pinned: boolean }) => {
      // Against an old server, persist the pin locally instead of PATCHing a
      // bare key it would store but the upgraded server would drop on read.
      if (!serverCanStorePins()) {
        // Throws if the local write fails (e.g. storage full) — a sync throw
        // here rejects the mutation, so `onError` rolls back the optimistic
        // patch rather than reporting a success that won't survive a reload.
        setLegacyPinnedConversationId(id, pinned);
        // Resolve with a minimal snapshot; the optimistic patch already moved
        // the row and there's no server row to reconcile.
        const labels = pinned ? { [PINNED_LABEL_KEY]: String(Date.now()) } : {};
        return Promise.resolve({ id, object: "conversation", labels } as Conversation);
      }
      return setConversationPinned(id, pinned);
    },
    // Move the row immediately — don't wait for the PATCH round-trip. Without
    // this the row lingers in its project folder (or the flat list) until the
    // network resolves, which reads as lag. Snapshot the pinned cache so a
    // failed PATCH rolls back.
    onMutate: ({ id, pinned }) => {
      const prevPinned =
        queryClient.getQueryData<PinnedConversationsResult>(PINNED_CONVERSATIONS_KEY);
      const base = findRow(id)?.labels ?? {};
      const labels: Record<string, string> = pinned
        ? { ...base, [PINNED_LABEL_KEY]: String(Date.now()) }
        : Object.fromEntries(Object.entries(base).filter(([k]) => k !== PINNED_LABEL_KEY));
      patch(id, labels, pinned);
      return { prevPinned };
    },
    onError: (_err, _vars, ctx) => {
      // Restore the pinned section; the label overlays on the other caches are
      // cosmetic and self-heal on the next reconcile.
      if (ctx?.prevPinned !== undefined) {
        queryClient.setQueryData(PINNED_CONVERSATIONS_KEY, ctx.prevPinned);
      }
    },
    onSuccess: (updated, { pinned }) => {
      // No `markConversationSeen` here: a pin PATCH writes only the label row,
      // never conversations.updated_at (unlike rename/archive/move, which bump
      // it and need the anchor to suppress that self-bump). Anchoring here would
      // instead mark a session with genuine unread activity as read on pin.
      //
      // Reconcile with the server-confirmed labels (authoritative pin
      // timestamp). Don't invalidate the pinned query — the `?pinned=true` label
      // index lags the write, so a refetch would momentarily revert the toggle;
      // the query's `staleTime` converges it later.
      patch(updated.id, updated.labels, pinned);
    },
  });
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
  /** Chosen emoji icon (a unicode grapheme), or null/absent for the default
      folder glyph. Sourced from the project's `config.icon`. */
  icon?: string | null;
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
 * Optimistic: the sidebar derives folder grouping from each row's `project_id`
 * (or legacy `omni_project` label), so `onMutate` overlays the new membership
 * onto every cached copy of the row and it regroups on the next frame — rather
 * than after the PATCH + list refetches round-trip, which parked the row in
 * its old section for the whole request (multi-second against a slow server).
 * The target id is read from the cached `["projects"]` list, the same data the
 * move UI rendered its targets from. A name with no cached first-class id yet
 * (label-only folder or brand-new name) is created over the network inside the
 * mutation, so that path skips the overlay and regroups on the reconcile
 * below, exactly as before.
 *
 * `onSuccess` still invalidates: membership pagination, project counts, and
 * ordering are server-owned, and those refetches start after the PATCH commits
 * so they confirm (not clobber) the overlay. `onError` restores the snapshot.
 */
export function useMoveToProject() {
  const queryClient = useQueryClient();

  // Write a membership (project_id + labels) onto the row in every list cache
  // that may hold it, plus the pinned-row backfill cache. Fields are overlaid
  // in place. Folder-page membership is a move: the target folder's cache
  // gains the row when nothing else could render it there (a row outside the
  // flat window has no other source — the sidebar unions folder pages with
  // the window's members), and folders the row just left drop it, but only
  // while it stays visible somewhere else — a briefly-stale old folder beats
  // the row vanishing from the sidebar until the reconcile refetches land.
  const overlayMembership = (
    row: Conversation,
    projectId: string | null | undefined,
    labels: Record<string, string>,
    targetProjectName: string | null,
  ) => {
    const id = row.id;
    const itemsById = new Map<string, SessionListWireItem>([
      [id, { id, project_id: projectId, labels }],
    ]);
    let inWindow = false;
    for (const [key, data] of queryClient.getQueriesData<ConversationsInfiniteData>({
      queryKey: ["conversations"],
    })) {
      const { data: next, found } = mergeItemsIntoPages(
        data,
        itemsById,
        filtersFromConversationQueryKey(key),
        undefined,
      );
      if (found.has(id)) inWindow = true;
      if (next !== data) queryClient.setQueryData(key, next);
    }
    // Target folder cache: overlay the row's fields if already present, and
    // insert a deep row (outside the flat window) into the first page.
    let visibleOutsideOldFolders = inWindow;
    if (targetProjectName !== null) {
      const targetKey = ["project-sessions", targetProjectName];
      const targetData = queryClient.getQueryData<ConversationsInfiniteData>(targetKey);
      if (targetData) {
        const { data: merged, found } = mergeItemsIntoPages(
          targetData,
          itemsById,
          PROJECT_FOLDER_FILTERS,
          undefined,
        );
        let next = merged ?? targetData;
        const [first, ...rest] = next.pages;
        if (!found.has(id) && !inWindow && first) {
          next = {
            ...next,
            pages: [
              { ...first, data: [{ ...row, project_id: projectId, labels }, ...first.data] },
              ...rest,
            ],
          };
        }
        if (next !== targetData) queryClient.setQueryData(targetKey, next);
        if (found.has(id) || first !== undefined) visibleOutsideOldFolders = true;
      }
    }
    if (visibleOutsideOldFolders) {
      for (const [key, data] of queryClient.getQueriesData<ConversationsInfiniteData>({
        queryKey: ["project-sessions"],
      })) {
        if (key[1] === targetProjectName) continue;
        const { data: next, removed } = removeIdsFromPages(data, new Set([id]));
        if (removed) queryClient.setQueryData(key, next);
      }
    }
    queryClient.setQueryData<Conversation | null>(["conversation-backfill", id], (old) =>
      old ? { ...old, project_id: projectId, labels } : old,
    );
  };

  return useMutation({
    mutationFn: ({ id, project }: { id: string; project: string }) =>
      moveConversationToProject(id, project),
    onMutate: async ({ id, project }) => {
      // `""` unfiles (no id needed); a file target must resolve to a cached
      // first-class id for the overlay to be truthful about the outcome. The
      // `?? undefined` folds a label-only folder's null id into "unknown".
      const target =
        project === ""
          ? null
          : (queryClient
              .getQueryData<ProjectSummary[]>(["projects"])
              ?.find((p) => p.name === project)?.id ?? undefined);
      if (target === undefined) return undefined;
      // Cancel in-flight list refetches so a response that started before the
      // overlay can't resolve after it and snap the row back to its old spot.
      await Promise.all([
        queryClient.cancelQueries({ queryKey: ["conversations"] }),
        queryClient.cancelQueries({ queryKey: ["project-sessions"] }),
      ]);
      const row = findCachedConversationRow(queryClient, id);
      if (!row) return undefined;
      // Wholesale snapshots for rollback: the overlay also drops the row from
      // other folders' pages, which a field-level revert couldn't restore.
      const previous = {
        conversations: queryClient.getQueriesData<ConversationsInfiniteData>({
          queryKey: ["conversations"],
        }),
        projectSessions: queryClient.getQueriesData<ConversationsInfiniteData>({
          queryKey: ["project-sessions"],
        }),
        backfill: queryClient.getQueryData<Conversation | null>(["conversation-backfill", id]),
      };
      const labels = Object.fromEntries(
        Object.entries(row.labels).filter(([labelKey]) => labelKey !== PROJECT_LABEL_KEY),
      );
      overlayMembership(row, target, labels, project === "" ? null : project);
      return previous;
    },
    onError: (_err, { id }, previous) => {
      if (previous === undefined) return;
      for (const [key, data] of [...previous.conversations, ...previous.projectSessions]) {
        if (data !== undefined) queryClient.setQueryData(key, data);
      }
      if (previous.backfill !== undefined) {
        queryClient.setQueryData(["conversation-backfill", id], previous.backfill);
      }
    },
    onSuccess: (updated) => {
      markConversationSeen(updated.id, updated.updated_at);
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      // Moving into/out of a project changes both folders' paginated lists.
      void queryClient.invalidateQueries({ queryKey: ["project-sessions"] });
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
  return withoutDeletingSessions((await res.json()) as ConversationsPage);
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
 * which has none). Throws a `BulkConversationMutationError` if any member
 * failed (e.g. a shared session the user can't modify), leaving those in place.
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
      if (failed.length > 0) {
        throw new BulkConversationMutationError("archive and unfile", {
          failed,
          succeeded,
          total: ids.length,
        });
      }
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
    mutationFn: ({ name, icon }: { name: string; icon?: string }) =>
      apiCreateProject(name, icon ? { icon } : undefined),
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
      // Return the resolved id so a caller promoting a label-only folder can
      // target the just-created row for a follow-up write instead of passing
      // the stale `null` and re-creating it (which 409s on the duplicate name).
      return projectId;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      void queryClient.invalidateQueries({ queryKey: ["project-sessions"] });
      void queryClient.invalidateQueries({ queryKey: ARCHIVED_PROJECT_NAMES_KEY });
    },
  });
}

/**
 * Fetch one project's full record (including its `config`) from
 * `GET /v1/projects/{id}`. Used by the settings editor to seed its fields, and
 * by the composer to read a project's stored session defaults. Disabled when
 * `id` is `null` (a label-only folder has no first-class row to read).
 */
export function useProjectConfig(id: string | null) {
  return useQuery<ProjectConfig>({
    queryKey: ["project-config", id],
    queryFn: async () => (await apiGetProject(id as string)).config ?? {},
    enabled: id !== null,
    staleTime: 30_000,
  });
}

/**
 * Replace a project's stored `config` defaults (`PATCH /v1/projects/{id}`).
 * A label-only folder (`id === null`) is promoted on demand — a row created
 * under its name — so its defaults can be stored, mirroring `useRenameProject`.
 * Passing `config: {}` clears the stored defaults.
 */
export function useUpdateProjectConfig() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      id,
      name,
      config,
    }: {
      id: string | null;
      name: string;
      config: ProjectConfig;
    }) => {
      const projectId = id ?? (await apiCreateProject(name)).id;
      return apiUpdateProjectConfig(projectId, config);
    },
    onSuccess: (project) => {
      // Seed the fresh config into the cache (not just invalidate) so the
      // composer's prefill reads the just-saved defaults on the very next visit
      // — the prefill settles once and would otherwise latch onto the stale
      // cached value during the 30s staleTime window while a refetch is still
      // in flight.
      queryClient.setQueryData<ProjectConfig>(["project-config", project.id], project.config ?? {});
      // Upsert the projects list too, so a just-promoted label-only folder
      // resolves to its NEW first-class id immediately (name → id is how the
      // composer keys the config lookup); a stale `id: null` would resolve the
      // config to `{}` and drop the saved defaults on that first visit.
      queryClient.setQueryData<ProjectSummary[]>(["projects"], (prev) => {
        if (!prev) return prev;
        const summary = { id: project.id, name: project.name, icon: project.config?.icon };
        return prev.some((p) => p.name === project.name)
          ? prev.map((p) => (p.name === project.name ? summary : p))
          : [...prev, summary];
      });
      void queryClient.invalidateQueries({ queryKey: ["project-config"] });
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
    },
  });
}
