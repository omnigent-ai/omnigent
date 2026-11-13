// Keep cached sub-agent tree lists (`["conversation", <parent>, "child_sessions"]`)
// in sync with `WS /v1/sessions/updates` frames.
//
// A frame row for a watched child carries everything a tree row shows except
// the message preview, so rows are patched in place. A refetch of the parent's
// list is scheduled only for what a patch can't place: a child the list doesn't
// hold yet, a changed child count, a rename or label change (tool, name and
// routed model derive from them server-side), a finished turn (its preview),
// or an older server whose rows lack the tree fields. A list that becomes
// observed again, and an observed list after a socket reconnect, refetch once,
// since frames that arrived while it was unwatched were never applied to it.

import type { Query, QueryClient } from "@tanstack/react-query";
import {
  type ChildSessionInfo,
  childSessionsQueryKey,
  parseChildSessionError,
} from "@/hooks/useChildSessions";
import { CODEX_NATIVE_SUBAGENT_WRAPPER, WRAPPER_LABEL_KEY } from "@/lib/nativeCodingAgents";
import type { SessionListWireItem } from "@/lib/sessionListCache";

/** Trailing delay that coalesces refetches of one parent's child list. */
export const CHILD_SESSIONS_REFETCH_DELAY_MS = 1_000;

// The server's default ``child_sessions`` page size, which ``fetchChildSessions``
// relies on. A full page may be truncated, so its length is no count baseline.
const CHILD_SESSIONS_PAGE_LIMIT = 20;

/** What the frame handler remembers between frames. */
export interface ChildFrameState {
  /** Last ``child_session_count`` per session. */
  counts: Map<string, number>;
  /** Last streamed labels per child, serialized, to spot a label change. */
  labels: Map<string, string>;
  /** Latest frame row per child and when it arrived. */
  frames: Map<string, { item: SessionListWireItem; at: number }>;
  /** In-flight list fetches by parent id: when each started, and the newest count streamed since. */
  fetchStarted: Map<string, { at: number; count?: number }>;
  /** Parents whose list must refetch now, not after the coalescing delay. */
  immediate: Set<string>;
}

/** @returns Empty frame-handler state. */
export function createChildFrameState(): ChildFrameState {
  return {
    counts: new Map(),
    labels: new Map(),
    frames: new Map(),
    fetchStarted: new Map(),
    immediate: new Set(),
  };
}

/**
 * Forget sessions that left the watch-set. A count is kept while its list
 * stays cached, so a sub-agent added while unwatched is caught on return.
 *
 * @param queryClient - The app QueryClient.
 * @param state - Frame-handler state, pruned in place.
 * @param watched - Ids the latest snapshot restated.
 */
export function pruneChildFrameState(
  queryClient: QueryClient,
  state: ChildFrameState,
  watched: Set<string>,
): void {
  for (const id of state.counts.keys()) {
    if (!watched.has(id) && !queryClient.getQueryData(childSessionsQueryKey(id))) {
      state.counts.delete(id);
    }
  }
  for (const map of [state.labels, state.frames]) {
    for (const id of map.keys()) if (!watched.has(id)) map.delete(id);
  }
}

// Labels the child summary reads for tool and session name, the harness glyph,
// routed model, and closed state. Usage counters such as
// `omnigent.last_context_tokens` change on every model call and are not listed.
const SUMMARY_LABEL_KEYS = new Set([
  "omnigent.wrapper",
  "omnigent.closed",
  "omnigent.routing.decision_id",
  "omnigent.claude_native.subagent_id",
  "omnigent.claude_native.description",
  "omnigent.codex_native.subagent_thread_id",
  "omnigent.codex_native.agent_nickname",
  "omnigent.codex_native.agent_role",
  "omnigent.codex_native.prompt",
  "omnigent.devin_native.subagent_agent_id",
  "omnigent.devin_native.subagent_title",
]);

function summaryLabels(labels: Record<string, string>): string {
  return JSON.stringify(
    Object.entries(labels)
      .filter(([key, value]) => SUMMARY_LABEL_KEYS.has(key) && value !== "")
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)),
  );
}

// A Codex child's tree label is its `tool`, derived server-side from these two
// labels (see `_codex_subagent_display_tool`). Mirrored so a rename patches in place.
const CODEX_NICKNAME_LABEL_KEY = "omnigent.codex_native.agent_nickname";
const CODEX_ROLE_LABEL_KEY = "omnigent.codex_native.agent_role";
const CODEX_DISPLAY_FALLBACK = "Codex";
const CODEX_DISPLAY_LABEL_KEYS = new Set([CODEX_NICKNAME_LABEL_KEY, CODEX_ROLE_LABEL_KEY]);

function codexDisplayTool(labels: Record<string, string>): string {
  return labels[CODEX_NICKNAME_LABEL_KEY] || labels[CODEX_ROLE_LABEL_KEY] || CODEX_DISPLAY_FALLBACK;
}

/** Whether a summary-label change can't be patched and needs a list refetch. */
function labelChangeNeedsRefetch(previous: string, current: string): boolean {
  const before = Object.fromEntries(JSON.parse(previous) as [string, string][]);
  const after = Object.fromEntries(JSON.parse(current) as [string, string][]);
  const keys = new Set([...Object.keys(before), ...Object.keys(after)]);
  for (const key of keys) {
    if (CODEX_DISPLAY_LABEL_KEYS.has(key)) continue;
    if (before[key] !== after[key]) return true;
  }
  return false;
}

function currentTaskStatus(
  ownStatus: SessionListWireItem["own_status"],
  hasError: boolean,
): string | null {
  // Mirrors the server's child summary: a recorded failure wins.
  if (hasError) return "failed";
  if (ownStatus === "running") return "in_progress";
  if (ownStatus === "idle") return "completed";
  if (ownStatus === "failed") return "failed";
  return null;
}

/**
 * Overlay one frame row onto its cached child-list row.
 *
 * @param row - The cached child summary.
 * @param item - The frame row for the same session.
 * @returns The patched row (the same object when nothing changed) and whether
 *   the parent's list must be refetched for fields a frame doesn't carry.
 */
function patchChildRow(
  row: ChildSessionInfo,
  item: SessionListWireItem,
): { row: ChildSessionInfo; refetch: boolean; immediate: boolean } {
  // An older server sends no own_status; `status` rolls up the child's own
  // children, so it can't stand in for `busy`.
  if (!("own_status" in item)) return { row, refetch: true, immediate: false };
  const busy = item.own_status === "running";
  const lastTaskError = parseChildSessionError(item.last_task_error);
  const patch: ChildSessionInfo = {
    ...row,
    busy,
    current_task_status: currentTaskStatus(item.own_status, lastTaskError !== null),
    last_task_error: lastTaskError,
    pending_elicitations_count: item.pending_elicitations_count ?? 0,
    ...("task_summary" in item ? { task_summary: item.task_summary ?? null } : {}),
  };
  // A Codex child's tool follows its nickname label, which the frame carries.
  if (item.labels?.[WRAPPER_LABEL_KEY] === CODEX_NATIVE_SUBAGENT_WRAPPER) {
    patch.tool = codexDisplayTool(item.labels);
  }
  // Tool and session name derive from the title server-side, so a title change
  // refetches now, without the coalescing delay. A finished turn moves the preview
  // and waits for the coalesced refetch.
  const titleChanged = item.title !== undefined && (item.title ?? null) !== row.title;
  if (titleChanged) patch.title = item.title ?? null;
  const refetch = row.busy && !busy;
  const changed =
    patch.title !== row.title ||
    patch.tool !== row.tool ||
    patch.busy !== row.busy ||
    patch.current_task_status !== row.current_task_status ||
    patch.pending_elicitations_count !== row.pending_elicitations_count ||
    patch.task_summary !== row.task_summary ||
    JSON.stringify(patch.last_task_error ?? null) !== JSON.stringify(row.last_task_error ?? null);
  return { row: changed ? patch : row, refetch, immediate: titleChanged };
}

/**
 * Apply frame rows to every cached child-session list they belong to.
 *
 * Lists that aren't cached are left alone: nothing displays them, and their
 * first fetch reads fresh rows.
 *
 * @param queryClient - The app QueryClient.
 * @param items - Rows from a snapshot/changed frame.
 * @param state - Frame-handler state, updated in place.
 * @param options.snapshot - The frame restates the whole watch-set (e.g. after
 *   a reconnect), so a row from an older server without
 *   ``child_session_count`` refetches its cached list to catch missed children.
 * @param options.now - Arrival time, e.g. ``Date.now()``.
 * @returns Parent ids whose child list needs a refetch.
 */
export function applyFrameToChildSessions(
  queryClient: QueryClient,
  items: SessionListWireItem[],
  state: ChildFrameState,
  { snapshot = false, now = Date.now() }: { snapshot?: boolean; now?: number } = {},
): Set<string> {
  const refetch = new Set<string>();
  const byParent = new Map<string, SessionListWireItem[]>();
  for (const item of items) {
    const listCached = queryClient.getQueryData(childSessionsQueryKey(item.id)) !== undefined;
    const count = item.child_session_count;
    if (typeof count === "number") {
      const previous = state.counts.get(item.id);
      state.counts.set(item.id, count);
      const flight = state.fetchStarted.get(item.id);
      if (flight) flight.count = count;
      if (previous !== undefined && previous !== count && listCached) refetch.add(item.id);
    } else if (snapshot && listCached) {
      refetch.add(item.id);
    }
    const parentId = item.parent_session_id;
    if (!parentId) continue;
    state.frames.set(item.id, { item, at: now });
    const group = byParent.get(parentId);
    if (group) group.push(item);
    else byParent.set(parentId, [item]);
  }
  for (const [parentId, group] of byParent) {
    const key = childSessionsQueryKey(parentId);
    const rows = queryClient.getQueryData<ChildSessionInfo[]>(key);
    for (const item of group) {
      if (item.labels === undefined) continue;
      const labels = summaryLabels(item.labels);
      const previous = state.labels.get(item.id);
      state.labels.set(item.id, labels);
      // Compared frame to frame: a change the cached row can't be patched for.
      if (
        rows !== undefined &&
        previous !== undefined &&
        previous !== labels &&
        labelChangeNeedsRefetch(previous, labels)
      ) {
        refetch.add(parentId);
      }
    }
    if (rows === undefined) continue;
    let next = rows;
    for (const item of group) {
      const index = next.findIndex((row) => row.id === item.id);
      if (index === -1) {
        // A sub-agent the list doesn't hold yet: only the server knows its
        // place and its summary fields.
        if (item.archived !== true) refetch.add(parentId);
        continue;
      }
      if (item.archived === true) {
        next = next.filter((row) => row.id !== item.id);
        continue;
      }
      const patched = patchChildRow(next[index], item);
      if (patched.refetch) refetch.add(parentId);
      if (patched.immediate) state.immediate.add(parentId);
      if (patched.row !== next[index]) {
        next = next.slice();
        next[index] = patched.row;
      }
    }
    if (next !== rows) queryClient.setQueryData<ChildSessionInfo[]>(key, next);
  }
  return refetch;
}

/**
 * Settle a freshly fetched child list against the stream.
 *
 * Seeds the parent's child-count baseline from the list's length, so a
 * sub-agent created between the fetch and the next frame triggers a refetch.
 * A count streamed during the fetch is newer than the response: it is kept,
 * and disagreement with the list returns true so the caller reconciles once.
 * Then re-applies frame rows that arrived while the fetch was in flight: the
 * response may predate them, and the stream won't resend an unchanged row.
 *
 * @param queryClient - The app QueryClient.
 * @param state - Frame-handler state; the parent's fetch start is consumed.
 * @param parentId - Session whose list was fetched, e.g. ``"conv_parent"``.
 * @returns Whether the list must be refetched to match the newer count.
 */
export function settleFetchedChildSessions(
  queryClient: QueryClient,
  state: ChildFrameState,
  parentId: string,
): boolean {
  const key = childSessionsQueryKey(parentId);
  const rows = queryClient.getQueryData<ChildSessionInfo[]>(key);
  const flight = state.fetchStarted.get(parentId);
  state.fetchStarted.delete(parentId);
  if (!Array.isArray(rows)) return false;
  const shortPage = rows.length < CHILD_SESSIONS_PAGE_LIMIT;
  const observed = flight?.count;
  if (observed !== undefined) state.counts.set(parentId, observed);
  else if (shortPage) state.counts.set(parentId, rows.length);
  const reconcile = observed !== undefined && shortPage && observed !== rows.length;
  if (flight === undefined) return false;
  const startedAt = flight.at;
  const next: ChildSessionInfo[] = [];
  for (const row of rows) {
    const frame = state.frames.get(row.id);
    if (!frame || frame.at < startedAt || frame.item.parent_session_id !== parentId) {
      next.push(row);
    } else if (frame.item.archived !== true) {
      next.push(patchChildRow(row, frame.item).row);
    }
  }
  const changed = next.length !== rows.length || next.some((row, index) => row !== rows[index]);
  if (changed) queryClient.setQueryData<ChildSessionInfo[]>(key, next);
  return reconcile;
}

/**
 * Whether a query is a sub-agent tree list with a mounted observer.
 *
 * @param query - A query from the cache.
 * @returns True for an observed ``["conversation", <id>, "child_sessions"]`` list.
 */
export function isObservedChildList(query: Query): boolean {
  return (
    query.queryKey[0] === "conversation" &&
    query.queryKey[2] === "child_sessions" &&
    query.getObserversCount() > 0
  );
}

/**
 * Drop removed (deleted or no longer accessible) sessions from every cached
 * child-session list.
 *
 * @param queryClient - The app QueryClient.
 * @param ids - Removed session ids.
 */
export function removeIdsFromChildSessions(queryClient: QueryClient, ids: string[]): void {
  const removed = new Set(ids);
  for (const [key, rows] of queryClient.getQueriesData<ChildSessionInfo[]>({
    queryKey: ["conversation"],
  })) {
    if (key[2] !== "child_sessions" || !Array.isArray(rows)) continue;
    if (!rows.some((row) => removed.has(row.id))) continue;
    queryClient.setQueryData<ChildSessionInfo[]>(
      key,
      rows.filter((row) => !removed.has(row.id)),
    );
  }
}

/** Per-parent coalescing refetcher for child-session lists. */
export interface ChildSessionsRefetcher {
  /** Refetch ``parentId``'s list after the delay, merged with pending requests. */
  schedule: (parentId: string) => void;
  /** Refetch ``parentId``'s list now, dropping any pending delayed refetch. */
  flush: (parentId: string) => void;
  /** Cancel every pending refetch. */
  dispose: () => void;
}

/**
 * Create a refetcher that issues at most one child-list refetch per parent
 * per ``delayMs``, however many frames ask for one.
 *
 * @param queryClient - The app QueryClient.
 * @param delayMs - Trailing delay, e.g. ``CHILD_SESSIONS_REFETCH_DELAY_MS``.
 * @returns The refetcher.
 */
export function createChildSessionsRefetcher(
  queryClient: QueryClient,
  delayMs: number = CHILD_SESSIONS_REFETCH_DELAY_MS,
): ChildSessionsRefetcher {
  const timers = new Map<string, ReturnType<typeof setTimeout>>();
  const refetchNow = (parentId: string) => {
    // Unmounted lists refetch too: they don't refetch on remount, and a
    // membership change is rare enough to pay for.
    void queryClient.invalidateQueries({
      queryKey: childSessionsQueryKey(parentId),
      exact: true,
      refetchType: "all",
    });
  };
  return {
    schedule(parentId) {
      if (timers.has(parentId)) return;
      timers.set(
        parentId,
        setTimeout(() => {
          timers.delete(parentId);
          refetchNow(parentId);
        }, delayMs),
      );
    },
    flush(parentId) {
      const timer = timers.get(parentId);
      if (timer !== undefined) clearTimeout(timer);
      timers.delete(parentId);
      refetchNow(parentId);
    },
    dispose() {
      for (const timer of timers.values()) clearTimeout(timer);
      timers.clear();
    },
  };
}
