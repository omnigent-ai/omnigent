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

/**
 * One task's run history (most-recent-first). `enabled` gates the fetch so an
 * unexpanded row costs nothing.
 */
export function useScheduledTaskRuns(id: string, enabled: boolean = true) {
  return useQuery<ScheduledTaskRun[]>({
    queryKey: scheduledTaskRunsKey(id),
    queryFn: () => listScheduledTaskRuns(id),
    enabled,
    staleTime: 30_000,
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

// Max attempts and interval for the post-run-now accelerated poll. The poll
// stops when the newest run reaches a terminal status, or after the cap (~20s
// ceiling — enough to cover the typical ~5s running→succeeded transition).
const RUN_NOW_POLL_MAX = 20;
const RUN_NOW_POLL_INTERVAL_MS = 1_000;

const TERMINAL_STATUSES = new Set(["succeeded", "failed", "skipped"]);

// Guard against stacking pollers when Run now is clicked rapidly. One active
// poller per task id at a time — the timer id is stored here and cleared when
// the poll finishes or yields to a newer one.
const runNowPollers: Map<string, ReturnType<typeof setTimeout>> = new Map();

/**
 * Cancel the accelerated run-now poller for a task id, if one is active: clears
 * its pending timer and drops it from the module-global map. Idempotent (a no-op
 * when there is no poller for the id). Call it from an unmount effect in any
 * component that fired a run-now so the self-scheduling `setTimeout` chain stops
 * issuing `refetchQueries` after the user navigates away — mirroring how
 * TaskDetailPage cancels its own awaiting-run timer on unmount. The poll is
 * already self-bounded (RUN_NOW_POLL_MAX / terminal status) and only touches the
 * unmount-safe queryClient, so this just avoids the wasted background refetches.
 */
export function cancelRunNowPoll(id: string): void {
  const timer = runNowPollers.get(id);
  if (timer != null) clearTimeout(timer);
  runNowPollers.delete(id);
}

/**
 * Trigger an immediate ("run now") fire of a task, then refresh the list and
 * that task's run history. The server returns 202 (fire-and-forget) and writes
 * the run row in the background as status "running", then transitions it to a
 * terminal status ("succeeded"/"failed"/"skipped") a few seconds later. A
 * single invalidate would miss both transitions. We kick off a bounded
 * accelerated poll: refetch every 1s until the newest run reaches a terminal
 * status, or after the 20-attempt cap (~20s ceiling), then final-invalidate and
 * stop. This ensures the spinner resolves and the unread dot appears once the
 * run completes.
 */
export function useRunScheduledTaskNow() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => runScheduledTaskNow(id),
    onSuccess: (_data, id) => {
      void queryClient.invalidateQueries({ queryKey: SCHEDULED_TASKS_KEY });
      void queryClient.invalidateQueries({ queryKey: scheduledTaskKey(id) });

      // Immediate invalidate so callers not on the detail page still see a fast
      // refresh (mirrors the old behaviour; the poll below is additive).
      void queryClient.invalidateQueries({ queryKey: scheduledTaskRunsKey(id) });

      // Snapshot the pre-fire newest-run id so we can identify the new row.
      const pre = queryClient.getQueryData<ScheduledTaskRun[]>(scheduledTaskRunsKey(id));
      const preNewestId = pre?.[0]?.id ?? null;

      // Cancel any in-flight poller for this task (double-click guard).
      const existing = runNowPollers.get(id);
      if (existing != null) clearTimeout(existing);

      let attempts = 0;

      function poll() {
        attempts += 1;
        void queryClient.refetchQueries({ queryKey: scheduledTaskRunsKey(id) }).then(() => {
          const current = queryClient.getQueryData<ScheduledTaskRun[]>(scheduledTaskRunsKey(id));
          // The newest run is index 0 (most-recent-first). We only stop once the
          // RUN WE JUST FIRED has reached a terminal status — a new row that is
          // still "running"/"scheduled" keeps the poll going.
          const newestRun = current?.[0];
          const isNewRun = newestRun != null && newestRun.id !== preNewestId;
          const isTerminal = isNewRun && TERMINAL_STATUSES.has(newestRun.status);
          if (isTerminal || attempts >= RUN_NOW_POLL_MAX) {
            // Terminal (or cap reached) — final invalidate and stop.
            runNowPollers.delete(id);
            void queryClient.invalidateQueries({ queryKey: scheduledTaskRunsKey(id) });
          } else {
            const timer = setTimeout(poll, RUN_NOW_POLL_INTERVAL_MS);
            runNowPollers.set(id, timer);
          }
        });
      }

      const timer = setTimeout(poll, RUN_NOW_POLL_INTERVAL_MS);
      runNowPollers.set(id, timer);
    },
  });
}
