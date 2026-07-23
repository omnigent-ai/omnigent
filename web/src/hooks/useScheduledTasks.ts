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
  listScheduledTaskRuns,
  listScheduledTasks,
  updateScheduledTask,
  type CreateScheduledTaskInput,
  type ScheduledTask,
  type ScheduledTaskRun,
  type UpdateScheduledTaskInput,
} from "@/lib/scheduledTasksApi";

/** Query key for the caller's scheduled-tasks list. */
export const SCHEDULED_TASKS_KEY = ["scheduled-tasks"] as const;

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
