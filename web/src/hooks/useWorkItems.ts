import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  deleteWorkItem,
  listWorkItems,
  updateWorkItem,
  type ListWorkItemsParams,
  type UpdateWorkItemPatch,
  type WorkItem,
} from "@/lib/workItemsApi";

const QUERY_KEY = ["work-items"];

/**
 * List work items (Tasks), newest first. Polls so agent-created items appear
 * without a manual refresh (the server has no work-item push channel yet).
 */
export function useWorkItems(params: ListWorkItemsParams = {}) {
  return useQuery<WorkItem[]>({
    queryKey: [...QUERY_KEY, params.status ?? null, params.conversationId ?? null],
    queryFn: () => listWorkItems(params),
    staleTime: 5_000,
    refetchInterval: 15_000,
  });
}

/** PATCH /v1/work-items/{id} — update status / pr_url / etc. */
export function useUpdateWorkItem() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: UpdateWorkItemPatch }) =>
      updateWorkItem(id, patch),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    },
  });
}

/** DELETE /v1/work-items/{id}. */
export function useDeleteWorkItem() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deleteWorkItem(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    },
  });
}
