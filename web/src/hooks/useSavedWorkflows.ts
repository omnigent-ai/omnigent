import { useCallback } from "react";
import { useQuery } from "@tanstack/react-query";

import { useServerInfo } from "@/lib/CapabilitiesContext";
import { authenticatedFetch } from "@/lib/identity";

/** One of the user's saved workflows, as `GET /v1/workflows` lists it. */
export interface SavedWorkflow {
  /** Workflow ID, a bare 32-character hex UUID. */
  id: string;
  /** One line, unique among the user's workflows, e.g. `"Triage flaky tests"`. */
  name: string;
  /** What it does and what a run needs, e.g. `"…Give it: the test suite."`. */
  description: string;
}

// The menu lists the user's workflows by name; more than this many need a search page.
const SAVED_WORKFLOWS_PAGE = 200;
const NO_WORKFLOWS: readonly SavedWorkflow[] = [];

/**
 * The user's saved workflows, by name, for Run a workflow. Empty on servers
 * without saved workflows.
 *
 * @param enabled - Whether to load them now, e.g. while the session offers workflows.
 * @returns The workflows, and `refresh`, which reloads them, e.g. as the "+" menu opens
 *   after the agent saved one.
 */
export function useSavedWorkflows({ enabled = true }: { enabled?: boolean } = {}): {
  workflows: readonly SavedWorkflow[];
  refresh: () => void;
} {
  const serverInfo = useServerInfo();
  const supported = serverInfo !== "loading" && serverInfo.workflows === true;
  const query = useQuery<SavedWorkflow[]>({
    queryKey: ["saved-workflows"],
    queryFn: async ({ signal }) => {
      const res = await authenticatedFetch(`/v1/workflows?limit=${SAVED_WORKFLOWS_PAGE}`, {
        signal,
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const body = (await res.json()) as { data?: SavedWorkflow[] };
      return (body.data ?? []).map(({ id, name, description }) => ({ id, name, description }));
    },
    staleTime: 30_000,
    enabled: enabled && supported,
  });
  const { refetch } = query;
  // refetch() ignores `enabled`, so a server without saved workflows is never asked.
  const refresh = useCallback(() => {
    if (enabled && supported) void refetch();
  }, [enabled, supported, refetch]);
  return { workflows: query.data ?? NO_WORKFLOWS, refresh };
}
