import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { SavedWorkflow } from "@/hooks/useSavedWorkflows";
import { authenticatedFetch } from "@/lib/identity";
import type { JournalEntry, JournalPlanStep } from "@/lib/sessionJournal";
import { withChosenTimeLimit, withSavedTimeLimits } from "@/lib/stepTimeLimit";
import type { WorkflowRun } from "@/lib/workflowRuns";
import type { WorkflowStep } from "@/lib/workflowGraphLayout";
import { ApiError, apiErrorFromResponse } from "@/lib/sessionsApi";

/** A saved workflow with its steps, as `GET /v1/workflows/{id}` returns it. */
export interface WorkflowDetail extends SavedWorkflow {
  steps: WorkflowStep[];
  planning_session_id?: string | null;
  /** Epoch seconds of its last write. */
  updated_at?: number;
  /**
   * The planning chat's proposal the description was saved from or kept over, until written
   * otherwise.
   */
  description_source?: string | null;
  /**
   * Epoch seconds when the description was last written; a write to the steps alone keeps it,
   * and another write can move it to that write's time. A server from before it sends none.
   */
  description_updated_at?: number | null;
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

/** The fields a server from before them refused as unknown. */
async function refusedFields(res: Response): Promise<unknown[]> {
  if (res.status !== 422) return [];
  const body = (await res
    .clone()
    .json()
    .catch(() => null)) as { detail?: unknown } | null;
  if (!Array.isArray(body?.detail)) return [];
  return body.detail.flatMap((issue: { type?: unknown; loc?: unknown }) =>
    issue?.type === "extra_forbidden" && Array.isArray(issue.loc) ? [issue.loc.at(-1)] : [],
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

/** A description saved with a plan, kept only while the stored one is `expected_description`. */
interface DescriptionSave {
  description?: string;
  expected_description?: string;
  description_source?: string;
}

interface PlanSave extends DescriptionSave {
  steps: WorkflowStep[];
  /** Save only while the stored steps are still these. */
  expected_steps?: WorkflowStep[];
}

async function savePlan(workflowId: string, proposal: PlanSave): Promise<WorkflowDetail> {
  const path = workflowPath(workflowId);
  const res = await sendWorkflow(path, "PATCH", proposal);
  const refused = await refusedFields(res);
  // A server from before conditional descriptions gets the steps alone, so its description stays.
  if (
    proposal.expected_description !== undefined &&
    refused.some((field) => field === "expected_description" || field === "description_source")
  )
    return writeWorkflow(path, "PATCH", { steps: proposal.steps });
  // One from before `expected_steps` writes the steps without that check.
  if (refused.includes("expected_steps")) {
    const { expected_steps: _, ...unchecked } = proposal;
    return savePlan(workflowId, unchecked);
  }
  return savedWorkflow(res);
}

/** Tries for a save whose steps change between its read and its write. */
const STORED_STEPS_SAVE_TRIES = 3;

/**
 * Save steps made by *edit* from the stored ones, read just before the write. The server takes
 * the write only while its steps are still the ones read; after a save in between, the edit is
 * redone over the newer steps, a few times.
 */
async function saveOverStoredSteps(
  workflowId: string,
  edit: (stored: WorkflowStep[]) => WorkflowStep[],
  description: DescriptionSave,
  tries = STORED_STEPS_SAVE_TRIES,
): Promise<WorkflowDetail> {
  const res = await authenticatedFetch(workflowPath(workflowId));
  if (!res.ok) throw await apiErrorFromResponse(res);
  const { steps: stored = [] } = (await res.json()) as WorkflowDetail;
  try {
    return await savePlan(workflowId, {
      ...description,
      steps: edit(stored),
      expected_steps: stored,
    });
  } catch (error) {
    if (!(error instanceof ApiError) || error.code !== "conflict") throw error;
    if (tries > 1) return saveOverStoredSteps(workflowId, edit, description, tries - 1);
    throw new Error("The plan kept changing while it saved. Try again.", { cause: error });
  }
}

/** After a failed save, the page may show an older plan, so it reads the saved one again. */
function useWorkflowReread(workflowId: string) {
  const queryClient = useQueryClient();
  return () => void queryClient.invalidateQueries({ queryKey: ["workflow", workflowId] });
}

/**
 * Apply the reviewed proposal, its steps and any new description, to the existing workflow.
 * Each step the workflow has keeps its time limit as saved when the write is made. The server
 * keeps a description that is no longer `expected_description`; a server too old to check that
 * gets the steps alone, so its description stays.
 */
export function useSaveWorkflowPlan(workflowId: string) {
  const onSuccess = useWorkflowSaved();
  const onError = useWorkflowReread(workflowId);
  return useMutation({
    mutationFn: ({ steps, ...description }: DescriptionSave & { steps: WorkflowStep[] }) =>
      saveOverStoredSteps(workflowId, (stored) => withSavedTimeLimits(steps, stored), description),
    onSuccess,
    onError,
  });
}

/**
 * Set a step's time limit, or clear it for null, from the step's menu. Only that step changes,
 * in the plan as saved when the write is made, so steps saved elsewhere stay; a description goes
 * as in {@link useSaveWorkflowPlan}.
 */
export function useSaveStepTimeLimit(workflowId: string) {
  const onSuccess = useWorkflowSaved();
  const onError = useWorkflowReread(workflowId);
  return useMutation({
    mutationFn: ({
      stepId,
      limit,
      ...description
    }: DescriptionSave & { stepId: string; limit: string | null }) =>
      saveOverStoredSteps(
        workflowId,
        (stored) => {
          if (!stored.some((step) => step.id === stepId))
            throw new Error("The step is no longer in the saved plan.");
          return stored.map((step) =>
            step.id === stepId ? { ...step, goal: withChosenTimeLimit(step.goal, limit) } : step,
          );
        },
        description,
      ),
    onSuccess,
    onError,
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
  sandbox_launching?: boolean;
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
  workflow_name?: string | null;
  started_by?: { session_id: string; title: string } | null;
  automation?: { id: string; name: string } | null;
  descendant_runs?: RawRun[];
  descendant_run_count?: number;
  descendant_runs_cursor?: string | null;
  descendant_runs_active?: boolean;
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
    sandboxLaunching: run.sandbox_launching ?? false,
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
    automation: run.automation ? { id: run.automation.id, name: run.automation.name } : undefined,
    workflowName: run.workflow_name,
    descendantRuns: (run.descendant_runs ?? []).map(toWorkflowRun),
    descendantRunCount: run.descendant_run_count,
    descendantRunsCursor: run.descendant_runs_cursor ?? null,
    descendantRunsActive: run.descendant_runs_active ?? false,
  };
}

async function runsPage(url: string, signal: AbortSignal): Promise<RunsPage> {
  const res = await authenticatedFetch(url, { signal });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as { data?: RawRun[]; next_cursor?: string | null };
  return {
    runs: (body.data ?? []).map(toWorkflowRun),
    nextCursor: body.next_cursor ?? null,
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
      return runsPage(`${workflowPath(workflowId)}/runs?${params}`, signal);
    },
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.nextCursor,
    enabled: workflowId !== "",
    staleTime: 10_000,
    // Other sessions and automations can start runs while this page stays open.
    refetchInterval: 10_000,
  });
}

/**
 * The workflow runs below *run* after the page it came with, a page at a time.
 *
 * @param workflowId - The workflow *run* follows.
 * @param run - The run, with its `descendantRunsCursor`.
 * @param enabled - Whether to load them, e.g. once asked to show more.
 */
export function useWorkflowRunDescendants(workflowId: string, run: WorkflowRun, enabled: boolean) {
  const cursor = run.descendantRunsCursor ?? null;
  return useInfiniteQuery({
    queryKey: ["workflow-run-descendants", workflowId, run.id, cursor],
    queryFn: ({ pageParam, signal }): Promise<RunsPage> => {
      const params = new URLSearchParams({ session_id: run.sessionId, limit: String(RUNS_PAGE) });
      if (pageParam) params.set("after", pageParam);
      const path = `${workflowPath(workflowId)}/runs/${encodeURIComponent(run.id)}/descendant_runs`;
      return runsPage(`${path}?${params}`, signal);
    },
    initialPageParam: cursor,
    getNextPageParam: (page) => page.nextCursor,
    enabled: enabled && cursor !== null,
    staleTime: 10_000,
    // Loaded pages keep up with their runs, as the first page does.
    refetchInterval: 10_000,
  });
}
