import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  deleteSchedule,
  listAllSchedules,
  listOnlineRunners,
  listSchedules,
  updateSchedule,
  type OnlineRunner,
  type Schedule,
  type UpdateSchedulePatch,
} from "@/lib/schedulesApi";

const KEY = ["schedules"];

/** List a conversation's loops & monitors. Polls so status/last-fired update. */
export function useSchedules(conversationId: string | undefined) {
  return useQuery<Schedule[]>({
    queryKey: [...KEY, conversationId ?? null],
    queryFn: () => listSchedules(conversationId as string),
    enabled: !!conversationId,
    staleTime: 5_000,
    refetchInterval: 15_000,
  });
}

/** List ALL schedules across the workspace (the global Schedules page). */
export function useAllSchedules() {
  return useQuery<Schedule[]>({
    queryKey: [...KEY, "all"],
    queryFn: () => listAllSchedules(),
    staleTime: 5_000,
    refetchInterval: 15_000,
  });
}

/** Poll currently-online runners — drives the "no runner → loops paused" hint. */
export function useOnlineRunners() {
  return useQuery<OnlineRunner[]>({
    queryKey: ["online-runners"],
    queryFn: () => listOnlineRunners(),
    staleTime: 5_000,
    refetchInterval: 15_000,
  });
}

/** PATCH /v1/schedules/{id} — enable/disable, rename, etc. */
export function useUpdateSchedule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: UpdateSchedulePatch }) =>
      updateSchedule(id, patch),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: KEY });
    },
  });
}

/** DELETE /v1/schedules/{id}. */
export function useDeleteSchedule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteSchedule(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: KEY });
    },
  });
}
