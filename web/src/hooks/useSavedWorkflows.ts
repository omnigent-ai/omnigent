import { useCallback } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isFeatureEnabled } from "@/lib/capabilities";
import { authenticatedFetch } from "@/lib/identity";

/** One of the user's saved workflows, as `GET /v1/workflows` lists it. */
export interface SavedWorkflow {
  /** Workflow ID, a bare 32-character hex UUID. */
  id: string;
  /** One line, unique among the user's workflows, e.g. `"Triage flaky tests"`. */
  name: string;
  /** What it does and what a run needs, e.g. `"…Give it: the test suite."`. */
  description: string;
  /** A draft has no saved plan and cannot run yet. */
  is_draft?: boolean;
}

// The menu lists the user's workflows by name; more than this many need a search page.
const SAVED_WORKFLOWS_PAGE = 200;
const NO_WORKFLOWS: readonly SavedWorkflow[] = [];
const SAVED_WORKFLOWS_KEY = ["saved-workflows"];

/**
 * The user's workflows, including drafts, by name. Empty on servers
 * without saved workflows.
 *
 * @param enabled - Whether to load them now, e.g. while the session offers workflows.
 * @returns The workflows; `isLoading` while the first load is in flight; `isError` when
 *   it failed; and `refresh`, which reloads them, e.g. as the "+" menu opens after the
 *   agent saved one.
 */
export function useSavedWorkflows({ enabled = true }: { enabled?: boolean } = {}): {
  workflows: readonly SavedWorkflow[];
  isLoading: boolean;
  isError: boolean;
  refresh: () => void;
} {
  const serverInfo = useServerInfo();
  const supported =
    isFeatureEnabled(serverInfo, "workflows") &&
    serverInfo !== "loading" &&
    serverInfo.workflows === true;
  const query = useQuery<SavedWorkflow[]>({
    queryKey: SAVED_WORKFLOWS_KEY,
    queryFn: async ({ signal }) => {
      const res = await authenticatedFetch(`/v1/workflows?limit=${SAVED_WORKFLOWS_PAGE}`, {
        signal,
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const body = (await res.json()) as { data?: SavedWorkflow[] };
      return (body.data ?? []).map(({ id, name, description, is_draft }) => ({
        id,
        name,
        description,
        is_draft,
      }));
    },
    staleTime: 30_000,
    enabled: enabled && supported,
  });
  const { refetch } = query;
  // refetch() ignores `enabled`, so a server without saved workflows is never asked.
  const refresh = useCallback(() => {
    if (enabled && supported) void refetch();
  }, [enabled, supported, refetch]);
  return {
    workflows: supported ? (query.data ?? NO_WORKFLOWS) : NO_WORKFLOWS,
    isLoading: query.isLoading,
    isError: query.isError,
    refresh,
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
