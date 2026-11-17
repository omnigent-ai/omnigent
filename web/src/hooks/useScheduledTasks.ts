// TanStack Query hooks over the `/v1/scheduled-tasks` client: a `useQuery` for
// the list + run history, and `useMutation`s for create / update / delete. Each
// mutation invalidates the list query on success so the Tasks page reflects the
// change without a manual refetch. Mirrors the pattern in `useConversations.ts`
// (invalidate-on-success), but the scheduled-tasks list reads the DB directly
// (no async search index), so a plain invalidate can't resurrect a just-deleted
// row — no patch-in-place gymnastics are needed here.

import {
  useInfiniteQuery,
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
  type InfiniteData,
  type QueryClient,
} from "@tanstack/react-query";
import { invalidateSessionGrants } from "@/hooks/usePermissions";
import { LIST_ROW_QUERY_ROOTS } from "@/lib/sessionListCache";
import {
  createScheduledTask,
  deleteScheduledTask,
  getScheduledTask,
  listScheduledTaskRunPullRequests,
  listScheduledTaskRuns,
  listScheduledTaskRunSteps,
  listScheduledTasks,
  listSharedScheduledTasks,
  runScheduledTaskNow,
  updateScheduledTask,
  type CreateScheduledTaskInput,
  type ScheduledTask,
  type SharedScheduledTasksPage,
  type ScheduledTaskRun,
  type ScheduledTaskRunPullRequests,
  type ScheduledTaskRunPullRequestsPage,
  type ScheduledTaskRunSteps,
  type UpdateScheduledTaskInput,
} from "@/lib/scheduledTasksApi";

/** Query key for the caller's scheduled-tasks list. */
export const SCHEDULED_TASKS_KEY = ["scheduled-tasks"] as const;

/** Query key for the automations other users shared with the caller. */
export const SHARED_SCHEDULED_TASKS_KEY = ["scheduled-tasks-shared"] as const;

/** Query key for one task's detail. */
export function scheduledTaskKey(id: string): readonly unknown[] {
  return ["scheduled-task", id];
}

/** Query key for one task's run history. */
export function scheduledTaskRunsKey(id: string): readonly unknown[] {
  return ["scheduled-task-runs", id];
}

/**
 * The caller's scheduled tasks. There is no push stream for scheduled tasks, so
 * a 30 s stale window keeps quick remounts cheap and a 60 s background poll
 * catches out-of-band changes (a task created from the agent tool, or a run
 * that just fired) without hammering the server.
 */
export function useScheduledTasks() {
  return useQuery<ScheduledTask[]>({
    queryKey: SCHEDULED_TASKS_KEY,
    queryFn: listScheduledTasks,
    staleTime: 30_000,
    // POLLING CONTRACT — read before reusing this hook.
    // The 60s interval only runs while a component that mounts this hook is
    // mounted; TanStack Query tears the interval down on unmount. Its consumers
    // are route-scoped pages, so polling stops the moment the user leaves them.
    // GUARD RAIL: do NOT mount this hook in a persistent / always-rendered spot
    // (sidebar, global layout, an app-shell badge). That would silently turn a
    // page-scoped poll into app-wide 60s background traffic. If you need a
    // persistent scheduled-tasks indicator, add a SEPARATE lightweight query
    // (no short refetchInterval, or a much longer one) — don't reuse this one.
    refetchInterval: 60_000,
  });
}

/**
 * The automations shared with the caller, a page at a time. Polls like
 * {@link useScheduledTasks}, under the same page-scoped contract: mount it only
 * on the Automations page. `enabled` is false where the server can't share them.
 */
export function useSharedScheduledTasks(enabled: boolean) {
  return useInfiniteQuery({
    queryKey: SHARED_SCHEDULED_TASKS_KEY,
    queryFn: ({ pageParam }) => listSharedScheduledTasks(pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.nextCursor,
    enabled,
    staleTime: 30_000,
    refetchInterval: 60_000,
  });
}

/** A task from the loaded pages of {@link useSharedScheduledTasks}, if any. */
function cachedSharedTask(queryClient: QueryClient, id: string): ScheduledTask | undefined {
  return queryClient
    .getQueryData<InfiniteData<SharedScheduledTasksPage>>(SHARED_SCHEDULED_TASKS_KEY)
    ?.pages.flatMap((page) => page.tasks)
    .find((task) => task.id === id);
}

/**
 * One task's detail (the `/tasks/:taskId` page). Seeds initial data from the
 * already-cached list so navigating from the list paints instantly, then
 * refetches to pick up any fields the list row didn't carry / staleness. The
 * seed is `placeholderData` (not `initialData`) so the query is still
 * considered stale and refetches immediately — a task edited elsewhere won't
 * show a frozen list snapshot. `enabled` gates the fetch (e.g. a missing id).
 */
export function useScheduledTask(id: string, enabled = true) {
  const queryClient = useQueryClient();
  return useQuery<ScheduledTask>({
    queryKey: scheduledTaskKey(id),
    queryFn: () => getScheduledTask(id),
    enabled: enabled && id !== "",
    staleTime: 30_000,
    // Paint from either list's cache while the detail fetch is in flight.
    placeholderData: () =>
      queryClient.getQueryData<ScheduledTask[]>(SCHEDULED_TASKS_KEY)?.find((t) => t.id === id) ??
      cachedSharedTask(queryClient, id),
  });
}

// Statuses a run can still move OUT of. Everything else (succeeded / failed /
// skipped, plus any status this client doesn't know) is treated as settled, so
// an unrecognised value can never pin the fast poll below on.
const ACTIVE_RUN_STATUSES = new Set(["scheduled", "starting", "running"]);

/** Live-refresh cadence for a task's run history while anything is in flight. */
const RUNS_REFETCH_INTERVAL_MS = 4_000;

/**
 * Cadence once everything has settled, so a scheduled run that starts later
 * shows up. Nothing pushes run changes to the page; the scheduler itself polls
 * every 30s.
 */
const RUNS_IDLE_REFETCH_INTERVAL_MS = 30_000;

/**
 * Whether a run list still has work in flight, i.e. a value the server is
 * expected to change on its own.
 *
 * TWO-PART, because a run row and its conversation settle at different times.
 * The row flips to a terminal status a beat BEFORE its conversation leaves
 * "running" and bumps `updated_at`, and the unread dot (`isConversationUnseen`)
 * keys off the CONVERSATION, not the row. Stopping on run-terminal alone would
 * end the refresh one step too early — the last fetch would still observe a
 * "running" conversation, leaving the dot grey until a remount refetched the
 * now-idle conversation. A run with no conversation (host-less skip/fail) never
 * gets one, so there is nothing to wait for.
 *
 * @param runs Run history as returned by the server (most-recent-first).
 * @returns `true` while any run — or any run's conversation — is unsettled.
 */
export function hasUnsettledRun(runs: ScheduledTaskRun[] | undefined): boolean {
  if (runs == null) return false;
  return runs.some(
    (run) =>
      ACTIVE_RUN_STATUSES.has(run.status) ||
      (run.conversationId != null && run.conversationStatus === "running"),
  );
}

/**
 * One task's run history (most-recent-first). `enabled` gates the fetch so an
 * unexpanded row costs nothing.
 *
 * POLLING CONTRACT — status-aware, and the ONLY thing that drives a run row to
 * its terminal state on-screen. Real runs take MINUTES (multi-minute agent turns
 * are routine), so a one-shot invalidate after "Run now" cannot settle the row:
 * the refresh has to outlive the run. While any run is unsettled
 * (`hasUnsettledRun`) we refetch every few seconds; once everything is terminal
 * the interval slows to `RUNS_IDLE_REFETCH_INTERVAL_MS`, so the next scheduled
 * run still appears on a page left open.
 *
 * Reads the stop condition from the query's OWN cached data, so it never feeds
 * back through the caller (same shape as `terminalsReconcileInterval` in
 * `useTerminals.ts`). Like every `refetchInterval`, it only runs while a
 * component mounting this hook is mounted — TanStack Query tears the interval
 * down on unmount, so navigating away ends it with no manual cleanup.
 *
 * A run row stuck in `running` (host died mid-turn) keeps the interval alive
 * while the page stays open, until a poll's read triggers the server's stale-run
 * backstop (`force_fail_stale_runs`) and fails the row. That backstop doesn't
 * settle the run's session, though: a session that stays running or waiting
 * keeps the page polling for as long as it's open.
 *
 * @param awaitingRun Whether a "Run now" was just fired and its row has not
 *     appeared yet. Load-bearing: `POST /run` returns 202 and writes the row a
 *     few seconds LATER, so the invalidate that follows it observes a history
 *     that is still entirely terminal. Without this the interval would evaluate
 *     to `false` right after a fire and never restart, so the new row would not
 *     appear at all. The caller is responsible for clearing the flag (on row
 *     arrival, or a safety timeout), which bounds this side of the condition.
 */
export function useScheduledTaskRuns(id: string, enabled = true, awaitingRun = false) {
  return useQuery<ScheduledTaskRun[]>({
    queryKey: scheduledTaskRunsKey(id),
    queryFn: () => listScheduledTaskRuns(id),
    enabled,
    staleTime: 30_000,
    refetchInterval: (query) =>
      awaitingRun || hasUnsettledRun(query.state.data)
        ? RUNS_REFETCH_INTERVAL_MS
        : RUNS_IDLE_REFETCH_INTERVAL_MS,
  });
}

/** How many runs one request for their plans covers. */
const RUN_STEPS_PAGE = 20;

/**
 * The plans an automation's runs started with, by run ID, for their timelines.
 * Asks for the given runs 20 at a time, paging as the run list does; only the
 * newest 20 poll, and only while one of them is in flight. Session updates also
 * refresh a page when a short continuation finishes between history polls.
 *
 * @param runs - The task's runs, most recent first, from `useScheduledTaskRuns`.
 * @param enabled - Whether to fetch: the server supports workflow history and the
 *     caller owns the automation, since a viewer's history has no plans.
 */
export function useScheduledTaskRunSteps(
  id: string,
  runs: readonly ScheduledTaskRun[] | undefined,
  enabled: boolean,
): ReadonlyMap<string, ScheduledTaskRunSteps> {
  const pages: ScheduledTaskRun[][] = [];
  for (let start = 0; start < (runs?.length ?? 0); start += RUN_STEPS_PAGE) {
    pages.push((runs ?? []).slice(start, start + RUN_STEPS_PAGE));
  }
  const newestUnsettled = hasUnsettledRun(pages[0]);
  return useQueries({
    queries: pages.map((page, index) => {
      // Each page starts after the previous page's last run, as the run list pages.
      const after = index === 0 ? undefined : pages[index - 1]!.at(-1)!.id;
      return {
        queryKey: [
          "scheduled-task-run-steps",
          id,
          after ?? null,
          page.map((run) => [
            run.id,
            run.status,
            run.conversationStatus,
            run.conversationUpdatedAt,
          ]),
        ],
        queryFn: () => listScheduledTaskRunSteps(id, { limit: RUN_STEPS_PAGE, after }),
        enabled,
        staleTime: 30_000,
        refetchInterval: index === 0 && newestUnsettled ? RUNS_REFETCH_INTERVAL_MS : false,
      };
    }),
    combine: (results) =>
      new Map(results.flatMap((result) => result.data ?? []).map((steps) => [steps.runId, steps])),
  });
}

/** How often a page whose PRs may still change asks again. */
const RUN_PULL_REQUESTS_REFETCH_MS = 60_000;
/** A run's last PR report can reach the server this long after the run finished. */
const RUN_PULL_REQUESTS_LATE_REPORT_MS = 5 * 60_000;

/**
 * Whether a page's PRs may still change without its runs changing: the server is
 * refreshing one of its sessions, a PR is open or of unknown state, or a run
 * finished recently enough that its last PR report may not have arrived.
 */
function runPullRequestsMayChange(
  runs: readonly ScheduledTaskRun[],
  page: ScheduledTaskRunPullRequestsPage | undefined,
  nowMs: number,
): boolean {
  if (page?.refreshPending) return true;
  if (
    page?.runs.some((run) => run.pullRequests.some((pr) => pr.state == null || pr.state === "OPEN"))
  )
    return true;
  return runs.some(
    (run) =>
      run.finishedAt != null && nowMs - run.finishedAt * 1000 < RUN_PULL_REQUESTS_LATE_REPORT_MS,
  );
}

/**
 * The PRs each of an automation's runs created or worked on, by run ID. Pages
 * 20 runs at a time like {@link useScheduledTaskRunSteps}. The server refreshes
 * sessions in the background and its PR reports don't change the runs, so a
 * page asks again each minute while its PRs may still change.
 *
 * @param runs - The task's runs, most recent first, from `useScheduledTaskRuns`.
 * @param enabled - Whether to fetch: the server lists run PRs and the caller owns
 *     the automation, since a viewer gets none.
 */
export function useScheduledTaskRunPullRequests(
  id: string,
  runs: readonly ScheduledTaskRun[] | undefined,
  enabled: boolean,
): ReadonlyMap<string, ScheduledTaskRunPullRequests> {
  const pages: ScheduledTaskRun[][] = [];
  for (let start = 0; start < (runs?.length ?? 0); start += RUN_STEPS_PAGE) {
    pages.push((runs ?? []).slice(start, start + RUN_STEPS_PAGE));
  }
  const newestUnsettled = hasUnsettledRun(pages[0]);
  return useQueries({
    queries: pages.map((page, index) => {
      const after = index === 0 ? undefined : pages[index - 1]!.at(-1)!.id;
      return {
        queryKey: [
          "scheduled-task-run-pull-requests",
          id,
          after ?? null,
          page.map((run) => [run.id, run.status, run.conversationUpdatedAt]),
        ],
        queryFn: () => listScheduledTaskRunPullRequests(id, { limit: RUN_STEPS_PAGE, after }),
        enabled,
        staleTime: 30_000,
        refetchInterval: (query: { state: { data?: ScheduledTaskRunPullRequestsPage } }) =>
          index === 0 && newestUnsettled
            ? RUNS_REFETCH_INTERVAL_MS
            : runPullRequestsMayChange(page, query.state.data, Date.now())
              ? RUN_PULL_REQUESTS_REFETCH_MS
              : false,
      };
    }),
    combine: (results) =>
      new Map(results.flatMap((result) => result.data?.runs ?? []).map((prs) => [prs.runId, prs])),
  });
}

/** Create a scheduled task, then refresh the list. */
export function useCreateScheduledTask() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: CreateScheduledTaskInput) => createScheduledTask(input),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: SCHEDULED_TASKS_KEY });
    },
  });
}

/** Update a task (pause/reactivate/rename/reschedule), then refresh the list. */
export function useUpdateScheduledTask() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, input }: { id: string; input: UpdateScheduledTaskInput }) =>
      updateScheduledTask(id, input),
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: SCHEDULED_TASKS_KEY });
      // Refresh the detail query so the /tasks/:id page reflects the edit.
      void queryClient.invalidateQueries({ queryKey: scheduledTaskKey(updated.id) });
      // A schedule/state change can shift the run history too (e.g. a
      // just-reactivated task), so refresh that task's runs if they're loaded.
      void queryClient.invalidateQueries({ queryKey: scheduledTaskRunsKey(updated.id) });
      // Switching to new sessions stops it managing its session's sharing.
      invalidateSessionGrants(queryClient);
    },
  });
}

/**
 * Refresh the cached session lists, and the sessions' own entries when known,
 * after an automation adds the session its runs start from or clears its label.
 */
export function refreshAutomationSourceSession(
  queryClient: QueryClient,
  sessionIds?: string | readonly string[] | null,
): void {
  for (const root of LIST_ROW_QUERY_ROOTS) {
    void queryClient.invalidateQueries({ queryKey: [root] });
  }
  void queryClient.invalidateQueries({ queryKey: ["projects"] });
  const ids = typeof sessionIds === "string" ? [sessionIds] : (sessionIds ?? []);
  for (const id of ids) void queryClient.invalidateQueries({ queryKey: ["session", id] });
}

/**
 * Drop deleted tasks from every cache, so a page can't render one from a cache
 * that is still fresh, then refresh the list.
 */
export function forgetScheduledTasks(queryClient: QueryClient, ids: string[]): void {
  queryClient.setQueryData<ScheduledTask[]>(SCHEDULED_TASKS_KEY, (tasks) =>
    tasks?.filter((t) => !ids.includes(t.id)),
  );
  for (const id of ids) {
    queryClient.removeQueries({ queryKey: scheduledTaskKey(id), exact: true });
    queryClient.removeQueries({ queryKey: scheduledTaskRunsKey(id), exact: true });
  }
  void queryClient.invalidateQueries({ queryKey: SCHEDULED_TASKS_KEY });
}

/** Delete a task, then refresh the list. */
export function useDeleteScheduledTask() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteScheduledTask(id),
    onSuccess: (_data, id) => {
      const deleted =
        queryClient.getQueryData<ScheduledTask>(scheduledTaskKey(id)) ??
        queryClient.getQueryData<ScheduledTask[]>(SCHEDULED_TASKS_KEY)?.find((t) => t.id === id);
      forgetScheduledTasks(queryClient, [id]);
      // A deleted automation no longer manages its session's sharing.
      invalidateSessionGrants(queryClient);
      // The server cleared the automation's label from the session it ran from,
      // so menus and delete warnings must stop naming the automation.
      if (deleted?.sourceSessionId) {
        refreshAutomationSourceSession(queryClient, deleted.sourceSessionId);
      }
    },
  });
}

/**
 * Trigger an immediate ("run now") fire of a task, then refresh the list and
 * that task's run history.
 *
 * The server returns 202 (fire-and-forget) and writes the run row in the
 * background as status "running", then transitions it to a terminal status
 * ("succeeded"/"failed"/"skipped") once the agent turn finishes — which for a
 * real run is MINUTES later. This mutation deliberately only invalidates: it
 * makes the new "running" row appear promptly and does NOT try to chase the
 * run to completion.
 *
 * `useScheduledTaskRuns` keeps polling the history until every run settles.
 */
export function useRunScheduledTaskNow() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => runScheduledTaskNow(id),
    onSuccess: (_data, id) => {
      void queryClient.invalidateQueries({ queryKey: SCHEDULED_TASKS_KEY });
      void queryClient.invalidateQueries({ queryKey: scheduledTaskKey(id) });
      // Surfaces the new "running" row immediately; the run-history
      // refetchInterval takes it from there.
      void queryClient.invalidateQueries({ queryKey: scheduledTaskRunsKey(id) });
    },
  });
}
