import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import type { SavedWorkflow } from "@/hooks/useSavedWorkflows";
import { authenticatedFetch } from "@/lib/identity";
import type { WorkflowRun } from "@/lib/workflowRuns";
import type { WorkflowStep } from "@/lib/workflowGraphLayout";

/** A saved workflow with its steps, as `GET /v1/workflows/{id}` returns it. */
export interface WorkflowDetail extends SavedWorkflow {
  steps: WorkflowStep[];
}

/** The workflow doesn't exist, or isn't the user's. */
export class WorkflowNotFoundError extends Error {
  constructor() {
    super("Workflow not found");
    this.name = "WorkflowNotFoundError";
  }
}

const RUNS_PAGE = 20;

function workflowPath(workflowId: string): string {
  return `/v1/workflows/${encodeURIComponent(workflowId)}`;
}

/**
 * One of the user's saved workflows, with its steps, for its page.
 *
 * @param workflowId - The workflow, e.g. from the page's URL.
 * @returns The query; a missing workflow fails with {@link WorkflowNotFoundError}.
 */
export function useWorkflow(workflowId: string) {
  return useQuery<WorkflowDetail>({
    queryKey: ["workflow", workflowId],
    queryFn: async ({ signal }) => {
      const res = await authenticatedFetch(workflowPath(workflowId), { signal });
      if (res.status === 404) throw new WorkflowNotFoundError();
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const { id, name, description, steps } = (await res.json()) as WorkflowDetail;
      return { id, name, description, steps: steps ?? [] };
    },
    enabled: workflowId !== "",
    // A missing workflow stays missing.
    retry: (failures, error) => !(error instanceof WorkflowNotFoundError) && failures < 2,
  });
}

interface RunsPage {
  runs: WorkflowRun[];
  nextCursor: string | null;
}

interface RawRun {
  id: string;
  session_id: string;
  title: string;
  started_at: number;
  session_status: WorkflowRun["sessionStatus"];
  archived: boolean;
  progress: {
    total: number;
    started: number;
    done: number;
    needs_input: number;
    failed: number;
  };
}

/**
 * The sessions that ran a saved workflow, newest first, a page at a time.
 *
 * @param workflowId - The workflow.
 * @returns An infinite query: `fetchNextPage` loads older runs while `hasNextPage`.
 */
export function useWorkflowRuns(workflowId: string) {
  return useInfiniteQuery({
    queryKey: ["workflow-runs", workflowId],
    queryFn: async ({ pageParam, signal }): Promise<RunsPage> => {
      const params = new URLSearchParams({ limit: String(RUNS_PAGE) });
      if (pageParam) params.set("before", pageParam);
      const res = await authenticatedFetch(`${workflowPath(workflowId)}/runs?${params}`, {
        signal,
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const body = (await res.json()) as { data?: RawRun[]; next_cursor?: string | null };
      return {
        runs: (body.data ?? []).map((run) => ({
          id: run.id,
          sessionId: run.session_id,
          title: run.title,
          startedAt: run.started_at,
          sessionStatus: run.session_status,
          archived: run.archived,
          progress: {
            total: run.progress.total,
            started: run.progress.started,
            done: run.progress.done,
            needsInput: run.progress.needs_input,
            failed: run.progress.failed,
          },
        })),
        nextCursor: body.next_cursor ?? null,
      };
    },
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.nextCursor,
    enabled: workflowId !== "",
    staleTime: 10_000,
  });
}
