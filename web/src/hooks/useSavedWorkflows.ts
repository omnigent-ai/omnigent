import { useCallback, useMemo } from "react";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { authenticatedFetch } from "@/lib/identity";
import { workflowSharingAvailable } from "@/lib/permissionsApi";

/** One of the user's saved workflows, as `GET /v1/workflows` lists it, or one shared with them. */
export interface SavedWorkflow {
  /** Workflow ID, a bare 32-character hex string. */
  id: string;
  /** One line, unique among its owner's workflows, e.g. `"Triage flaky tests"`. */
  name: string;
  /** What it does and what a run needs, e.g. `"…Give it: the test suite."`. */
  description: string;
  /** A draft has no saved plan and cannot run yet. */
  is_draft?: boolean;
  /** `"viewer"` on a workflow someone shared with the user, who may view, run and copy it. */
  viewer_role?: "owner" | "viewer";
  /** The owner's name, on a workflow shared with the user. */
  owner_display_name?: string | null;
  /** `false` when the server can't keep its grants, e.g. one saved before sharing was available. */
  shareable?: boolean;
}

export interface WorkflowPagination {
  hasMore: boolean;
  loadingMore: boolean;
  loadMore: () => void;
}

interface WorkflowPage {
  data: SavedWorkflow[];
  has_more: boolean;
  last_id: string | null;
  next_cursor?: string | null;
}

interface SharedWorkflowPage {
  data: SavedWorkflow[];
  next_cursor: string | null;
}

const SAVED_WORKFLOWS_PAGE = 200;
const SHARED_WORKFLOWS_PAGE = 100;
const NO_WORKFLOWS: readonly SavedWorkflow[] = [];
const SAVED_WORKFLOWS_KEY = ["saved-workflows"];
const SHARED_WORKFLOWS_KEY = ["shared-workflows"];

/**
 * The workflows other people shared with the user, ordered by ID. Empty on servers
 * that don't share workflows.
 *
 * @param enabled - Whether to load them now.
 * @returns The workflows, each with `viewer_role` `"viewer"` and its owner's name;
 *   `isLoading`, `isError`, `refresh` and `pagination` as for {@link useSavedWorkflows}.
 */
export function useSharedWorkflows({ enabled = true }: { enabled?: boolean } = {}): {
  workflows: readonly SavedWorkflow[];
  isLoading: boolean;
  isError: boolean;
  refresh: () => void;
  pagination: WorkflowPagination;
} {
  const serverInfo = useServerInfo();
  const supported =
    isFeatureEnabled(serverInfo, "workflows") && workflowSharingAvailable(serverInfo);
  const query = useInfiniteQuery({
    queryKey: SHARED_WORKFLOWS_KEY,
    queryFn: async ({ signal, pageParam }): Promise<SharedWorkflowPage> => {
      const params = new URLSearchParams({ limit: String(SHARED_WORKFLOWS_PAGE) });
      if (pageParam) params.set("after", pageParam);
      const res = await authenticatedFetch(`/v1/workflows/shared?${params}`, { signal });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      return (await res.json()) as SharedWorkflowPage;
    },
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    staleTime: 30_000,
    enabled: enabled && supported,
  });
  const workflows = useMemo(
    () => query.data?.pages.flatMap((page) => page.data) ?? NO_WORKFLOWS,
    [query.data],
  );
  const { refetch } = query;
  // refetch() ignores `enabled`, so a server that doesn't share workflows is never asked.
  const refresh = useCallback(() => {
    if (enabled && supported) void refetch();
  }, [enabled, supported, refetch]);
  return {
    workflows: supported ? workflows : NO_WORKFLOWS,
    isLoading: supported && query.isLoading,
    isError: supported && query.isError,
    refresh,
    pagination: {
      hasMore: supported && query.hasNextPage,
      loadingMore: query.isFetchingNextPage,
      loadMore: () => {
        if (enabled && supported && query.hasNextPage && !query.isFetchingNextPage) {
          void query.fetchNextPage();
        }
      },
    },
  };
}

/**
 * The user's workflows, including drafts, by name. Empty on servers
 * without saved workflows.
 *
 * @param enabled - Whether to load them now, e.g. while the session offers workflows.
 * @param includeShared - Also list the workflows shared with the user, after their own,
 *   e.g. for Run a workflow.
 * @returns The workflows; `isLoading` while the first load is in flight; `isError` when
 *   it failed; and `refresh`, which reloads them, e.g. as the "+" menu opens after the
 *   agent saved one.
 */
export function useSavedWorkflows({
  enabled = true,
  includeShared = false,
}: { enabled?: boolean; includeShared?: boolean } = {}): {
  workflows: readonly SavedWorkflow[];
  isLoading: boolean;
  isError: boolean;
  refresh: () => void;
  pagination: WorkflowPagination;
} {
  const serverInfo = useServerInfo();
  const supported =
    isFeatureEnabled(serverInfo, "workflows") &&
    serverInfo !== "loading" &&
    serverInfo.workflows === true;
  const query = useInfiniteQuery({
    queryKey: SAVED_WORKFLOWS_KEY,
    queryFn: async ({ signal, pageParam }): Promise<WorkflowPage> => {
      const params = new URLSearchParams({ limit: String(SAVED_WORKFLOWS_PAGE) });
      if (pageParam) params.set("after", pageParam);
      const res = await authenticatedFetch(`/v1/workflows?${params}`, {
        signal,
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      return (await res.json()) as WorkflowPage;
    },
    initialPageParam: null as string | null,
    getNextPageParam: (last) =>
      last.has_more ? (last.next_cursor ?? last.last_id ?? undefined) : undefined,
    staleTime: 30_000,
    enabled: enabled && supported,
  });
  const shared = useSharedWorkflows({ enabled: enabled && includeShared });
  const { refetch } = query;
  const refreshShared = shared.refresh;
  // refetch() ignores `enabled`, so a server without saved workflows is never asked.
  // The shared list reloads too, e.g. one shared since the "+" menu last opened.
  const refresh = useCallback(() => {
    if (enabled && supported) void refetch();
    if (includeShared) refreshShared();
  }, [enabled, supported, refetch, includeShared, refreshShared]);
  const owned = useMemo(
    () => query.data?.pages.flatMap((page) => page.data) ?? NO_WORKFLOWS,
    [query.data],
  );
  const sharedWorkflows = includeShared ? shared.workflows : NO_WORKFLOWS;
  const workflows = useMemo(
    () => (sharedWorkflows.length > 0 ? [...owned, ...sharedWorkflows] : owned),
    [owned, sharedWorkflows],
  );
  const ownedHasMore = supported && query.hasNextPage;
  return {
    workflows: supported ? workflows : NO_WORKFLOWS,
    isLoading: query.isLoading,
    isError: query.isError,
    refresh,
    pagination: {
      // The user's own workflows load first, then the rest of the shared ones.
      hasMore: ownedHasMore || (includeShared && shared.pagination.hasMore),
      loadingMore: query.isFetchingNextPage || shared.pagination.loadingMore,
      loadMore: () => {
        if (ownedHasMore) {
          if (enabled && !query.isFetchingNextPage) void query.fetchNextPage();
        } else if (includeShared) {
          shared.pagination.loadMore();
        }
      },
    },
  };
}

/** Deletes one of the user's saved workflows by ID, then reloads the list. */
export function useDeleteWorkflow() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      const res = await authenticatedFetch(`/v1/workflows/${encodeURIComponent(id)}`, {
        method: "DELETE",
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: SAVED_WORKFLOWS_KEY }),
  });
}
