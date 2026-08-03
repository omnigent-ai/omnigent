// TanStack Query hooks over the `/v1/scheduled-tasks` client: a `useQuery` for
// the list + run history, and `useMutation`s for create / update / delete. Each
// mutation invalidates the list query on success so the Tasks page reflects the
// change without a manual refetch. Mirrors the pattern in `useConversations.ts`
// (invalidate-on-success), but the scheduled-tasks list reads the DB directly
// (no async search index), so a plain invalidate can't resurrect a just-deleted
// row — no patch-in-place gymnastics are needed here.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createScheduledTask,
  deleteScheduledTask,
  getScheduledTask,
  listScheduledTaskRuns,
  listScheduledTasks,
  runScheduledTaskNow,
  updateScheduledTask,
  type CreateScheduledTaskInput,
  type ScheduledTask,
  type ScheduledTaskRun,
  type UpdateScheduledTaskInput,
} from "@/lib/scheduledTasksApi";

/** Query key for the caller's scheduled-tasks list. */
export const SCHEDULED_TASKS_KEY = ["scheduled-tasks"] as const;

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
    // mounted; TanStack Query tears the interval down on unmount. Today the sole
    // consumer is TasksPage, which is route-scoped and lazy-loaded at /tasks — so
    // polling happens ONLY while the user is on the Scheduled Tasks page and
    // stops the moment they navigate away.
    // GUARD RAIL: do NOT mount this hook in a persistent / always-rendered spot
    // (sidebar, global layout, an app-shell badge). That would silently turn a
    // page-scoped poll into app-wide 60s background traffic. If you need a
    // persistent scheduled-tasks indicator, add a SEPARATE lightweight query
    // (no short refetchInterval, or a much longer one) — don't reuse this one.
    refetchInterval: 60_000,
  });
}

/**
 * One task's detail (the `/tasks/:taskId` page). Seeds initial data from the
 * already-cached list so navigating from the list paints instantly, then
 * refetches to pick up any fields the list row didn't carry / staleness. The
 * seed is `placeholderData` (not `initialData`) so the query is still
 * considered stale and refetches immediately — a task edited elsewhere won't
 * show a frozen list snapshot. `enabled` gates the fetch (e.g. a missing id).
 */
export function useScheduledTask(id: string, enabled: boolean = true) {
  const queryClient = useQueryClient();
  return useQuery<ScheduledTask>({
    queryKey: scheduledTaskKey(id),
    queryFn: () => getScheduledTask(id),
    enabled: enabled && id !== "",
    staleTime: 30_000,
    // Paint from the list cache while the detail fetch is in flight.
    placeholderData: () =>
      queryClient.getQueryData<ScheduledTask[]>(SCHEDULED_TASKS_KEY)?.find((t) => t.id === id),
  });
}

// Statuses a run can still move OUT of. Everything else (succeeded / failed /
// skipped, plus any status this client doesn't know) is treated as settled, so
// an unrecognised value can never strand the poll below in an infinite loop.
const ACTIVE_RUN_STATUSES = new Set(["running", "scheduled"]);

/** Live-refresh cadence for a task's run history while anything is in flight. */
const RUNS_REFETCH_INTERVAL_MS = 4_000;

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
 * the interval returns `false` and polling STOPS — no steady-state traffic.
 *
 * Reads the stop condition from the query's OWN cached data, so it never feeds
 * back through the caller (same shape as `terminalsReconcileInterval` in
 * `useTerminals.ts`). Like every `refetchInterval`, it only runs while a
 * component mounting this hook is mounted — TanStack Query tears the interval
 * down on unmount, so navigating away ends it with no manual cleanup.
 *
 * A run stuck in `running` (host died mid-turn) keeps the interval alive while
 * the page stays open. That is deliberate and self-limiting: each poll is a READ
 * of the runs endpoint, which is exactly what triggers the server's
 * lazy-on-read stale-run backstop (`force_fail_stale_runs`) that force-fails the
 * row and lets polling stop.
 *
 * @param awaitingRun Whether a "Run now" was just fired and its row has not
 *     appeared yet. Load-bearing: `POST /run` returns 202 and writes the row a
 *     few seconds LATER, so the invalidate that follows it observes a history
 *     that is still entirely terminal. Without this the interval would evaluate
 *     to `false` right after a fire and never restart, so the new row would not
 *     appear at all. The caller is responsible for clearing the flag (on row
 *     arrival, or a safety timeout), which bounds this side of the condition.
 */
export function useScheduledTaskRuns(
  id: string,
  enabled: boolean = true,
  awaitingRun: boolean = false,
) {
  return useQuery<ScheduledTaskRun[]>({
    queryKey: scheduledTaskRunsKey(id),
    queryFn: () => listScheduledTaskRuns(id),
    enabled,
    staleTime: 30_000,
    refetchInterval: (query) =>
      awaitingRun || hasUnsettledRun(query.state.data) ? RUNS_REFETCH_INTERVAL_MS : false,
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
    },
  });
}

/** Delete a task, then refresh the list. */
export function useDeleteScheduledTask() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteScheduledTask(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: SCHEDULED_TASKS_KEY });
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
 * Driving the row to its terminal state is `useScheduledTaskRuns`'s
 * status-aware `refetchInterval`, which keeps refreshing for as long as the run
 * is unsettled and stops when it isn't. That replaced an accelerated poll that
 * lived here: a mutation-scoped poll has to guess a time budget up front, and
 * any budget short enough to avoid hammering the server was far too short for a
 * multi-minute run — it gave up mid-run and left the row spinning until a
 * remount refetched it. The query-level interval has no budget to exhaust.
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
