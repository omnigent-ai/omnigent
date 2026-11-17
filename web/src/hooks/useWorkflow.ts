import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { SavedWorkflow } from "@/hooks/useSavedWorkflows";
import { authenticatedFetch } from "@/lib/identity";
import type { JournalEntry, JournalPlanStep } from "@/lib/sessionJournal";
import type { WorkflowRun } from "@/lib/workflowRuns";
import type { WorkflowStep } from "@/lib/workflowGraphLayout";
import { ApiError, apiErrorFromResponse } from "@/lib/sessionsApi";

/** A saved workflow with its steps, as `GET /v1/workflows/{id}` returns it. */
export interface WorkflowDetail extends SavedWorkflow {
  steps: WorkflowStep[];
  planning_session_id?: string | null;
  /** Epoch seconds of its last write. */
  updated_at?: number;
  /** The planning chat's proposal the description was saved from, until written otherwise. */
  description_source?: string | null;
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
      const workflow = (await res.json()) as WorkflowDetail;
      return { ...workflow, steps: workflow.steps ?? [] };
    },
    enabled: workflowId !== "",
    // A missing workflow stays missing.
    retry: (failures, error) => !(error instanceof WorkflowNotFoundError) && failures < 2,
  });
}

function sendWorkflow(path: string, method: string, body: object): Promise<Response> {
  return authenticatedFetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function savedWorkflow(res: Response): Promise<WorkflowDetail> {
  if (!res.ok) throw await apiErrorFromResponse(res);
  return res.json() as Promise<WorkflowDetail>;
}

async function writeWorkflow(path: string, method: string, body: object): Promise<WorkflowDetail> {
  return savedWorkflow(await sendWorkflow(path, method, body));
}

/** Whether a server from before conditional descriptions refused their fields as unknown. */
async function refusesExpectedDescription(res: Response): Promise<boolean> {
  if (res.status !== 422) return false;
  const body = (await res
    .clone()
    .json()
    .catch(() => null)) as { detail?: unknown } | null;
  return (
    Array.isArray(body?.detail) &&
    body.detail.some(
      (issue: { type?: unknown; loc?: unknown }) =>
        issue?.type === "extra_forbidden" &&
        Array.isArray(issue.loc) &&
        ["expected_description", "description_source"].includes(issue.loc.at(-1)),
    )
  );
}

function useWorkflowSaved() {
  const queryClient = useQueryClient();
  return (workflow: WorkflowDetail) => {
    queryClient.setQueryData(["workflow", workflow.id], workflow);
    void queryClient.invalidateQueries({ queryKey: ["saved-workflows"] });
  };
}

/** Save a draft before launching its planning chat. */
export function useCreateWorkflowDraft() {
  const onSuccess = useWorkflowSaved();
  return useMutation({
    mutationFn: (draft: { name: string; description: string }) =>
      writeWorkflow("/v1/workflows", "POST", { ...draft, steps: [] }),
    onSuccess,
  });
}

/** Copy the saved plan into a new workflow, without its planning chat or runs. */
export function useCloneWorkflow() {
  const onSuccess = useWorkflowSaved();
  return useMutation({
    mutationFn: async (workflowId: string) => {
      const res = await authenticatedFetch(workflowPath(workflowId));
      if (!res.ok) throw await apiErrorFromResponse(res);
      const workflow = (await res.json()) as WorkflowDetail;
      for (let copy = 1; ; copy += 1) {
        const suffix = copy === 1 ? " (copy)" : ` (copy ${copy})`;
        const name =
          [...workflow.name]
            .slice(0, 80 - suffix.length)
            .join("")
            .trimEnd() + suffix;
        try {
          // A name conflict must resolve before trying the next name.
          // oxlint-disable-next-line no-await-in-loop
          return await writeWorkflow("/v1/workflows", "POST", {
            name,
            description: workflow.description,
            steps: workflow.steps,
          });
        } catch (error) {
          if (!(error instanceof ApiError) || error.status !== 409) throw error;
        }
      }
    },
    onSuccess,
  });
}

/**
 * Apply the reviewed proposal, its steps and any new description, to the existing workflow.
 * The server keeps a description that is no longer `expected_description`; a server too old
 * to check that gets the steps alone, so its description stays.
 */
export function useSaveWorkflowPlan(workflowId: string) {
  const onSuccess = useWorkflowSaved();
  return useMutation({
    mutationFn: async (proposal: {
      steps: WorkflowStep[];
      description?: string;
      expected_description?: string;
      description_source?: string;
    }) => {
      const path = workflowPath(workflowId);
      const res = await sendWorkflow(path, "PATCH", proposal);
      if (proposal.expected_description !== undefined && (await refusesExpectedDescription(res)))
        return writeWorkflow(path, "PATCH", { steps: proposal.steps });
      return savedWorkflow(res);
    },
    onSuccess,
  });
}

/** Replace the workflow's description with the user's own wording. */
export function useSaveWorkflowDescription(workflowId: string) {
  const onSuccess = useWorkflowSaved();
  return useMutation({
    mutationFn: (description: string) =>
      writeWorkflow(workflowPath(workflowId), "PATCH", { description }),
    onSuccess,
  });
}

/** Persist the planning chat so reopening the workflow restores it on any device. */
export function useSetWorkflowPlanningSession(workflowId: string) {
  const onSuccess = useWorkflowSaved();
  return useMutation({
    mutationFn: (planningSessionId: string) =>
      writeWorkflow(workflowPath(workflowId), "PATCH", { planning_session_id: planningSessionId }),
    onSuccess,
  });
}

interface RunsPage {
  runs: WorkflowRun[];
  nextCursor: string | null;
}

interface RawRun {
  id: string;
  plan_graph_revision?: string | number | null;
  session_id: string;
  title: string;
  started_at: number;
  is_current?: boolean;
  session_status: WorkflowRun["sessionStatus"];
  background_work_running?: boolean;
  archived: boolean;
  progress: {
    total: number;
    started: number;
    done: number;
    needs_input: number;
    failed: number;
  };
  steps?: JournalPlanStep[];
  statuses?: JournalEntry[];
  workflow_id?: string;
  started_by?: { session_id: string; title: string } | null;
  descendant_runs?: RawRun[];
}

function toWorkflowRun(run: RawRun): WorkflowRun {
  return {
    id: run.id,
    workflowId: run.workflow_id,
    planGraphRevision: run.plan_graph_revision ?? undefined,
    sessionId: run.session_id,
    title: run.title,
    startedAt: run.started_at,
    isCurrent: run.is_current === true,
    sessionStatus: run.session_status,
    backgroundWorkRunning: run.background_work_running ?? false,
    archived: run.archived,
    progress: {
      total: run.progress.total,
      started: run.progress.started,
      done: run.progress.done,
      needsInput: run.progress.needs_input,
      failed: run.progress.failed,
    },
    steps: run.steps ?? [],
    statuses: run.statuses ?? [],
    startedBy: run.started_by
      ? { sessionId: run.started_by.session_id, title: run.started_by.title }
      : undefined,
    descendantRuns: (run.descendant_runs ?? []).map(toWorkflowRun),
  };
}

/**
 * The sessions that ran a saved workflow, newest first, a page at a time.
 *
 * @param workflowId - The workflow.
 * @param sessionId - Only that session's runs, e.g. for its Tasks tab.
 * @returns An infinite query: `fetchNextPage` loads older runs while `hasNextPage`.
 */
export function useWorkflowRuns(workflowId: string, sessionId?: string) {
  return useInfiniteQuery({
    queryKey: sessionId ? ["workflow-runs", workflowId, sessionId] : ["workflow-runs", workflowId],
    queryFn: async ({ pageParam, signal }): Promise<RunsPage> => {
      const params = new URLSearchParams({ limit: String(RUNS_PAGE) });
      if (pageParam) params.set("before", pageParam);
      if (sessionId) params.set("session_id", sessionId);
      const res = await authenticatedFetch(`${workflowPath(workflowId)}/runs?${params}`, {
        signal,
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const body = (await res.json()) as { data?: RawRun[]; next_cursor?: string | null };
      return {
        runs: (body.data ?? []).map(toWorkflowRun),
        nextCursor: body.next_cursor ?? null,
      };
    },
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.nextCursor,
    enabled: workflowId !== "",
    staleTime: 10_000,
    // Other sessions and automations can start runs while this page stays open.
    refetchInterval: 10_000,
  });
}
