import {
  isPlanComplete,
  type JournalEntry,
  type JournalPlanStep,
  type StepProgress,
  stepProgress,
} from "@/lib/sessionJournal";

/** One run of a saved workflow: a session that started it. */
export interface WorkflowRun {
  /** The ID of the plan entry that started the run. */
  id: string;
  /** Graph ID, or earliest unchanged legacy graph position; absent on older servers. */
  planGraphRevision?: string | number;
  sessionId: string;
  /** The session's title; empty when it has none yet. */
  title: string;
  /** Unix epoch seconds when the run's plan was written. */
  startedAt: number;
  /** Only the current run can use its session's activity and accept actions. */
  isCurrent: boolean;
  sessionStatus: "idle" | "running" | "failed";
  /** Work that outlives the parent turn, such as background shells or an active goal. */
  backgroundWorkRunning?: boolean;
  /** The session's sandbox is still launching, so the run is starting; absent on older servers. */
  sandboxLaunching?: boolean;
  archived: boolean;
  /** The run's latest steps, counted by their current status. */
  progress: {
    total: number;
    started: number;
    done: number;
    needsInput: number;
    failed: number;
  };
  /** The latest plan's steps within this invocation. */
  steps: JournalPlanStep[];
  /** Each of those steps' newest status entry. */
  statuses: JournalEntry[];
  /** The saved workflow the run follows; absent on older servers. */
  workflowId?: string;
  /** The session that started the run, when another session did and you can read it. */
  startedBy?: WorkflowRunStarter;
  /** The automation whose scheduled run started the run's session, when you can see it. */
  automation?: WorkflowRunAutomation;
  /** Its workflow's name when the workflow is yours; `null` for someone else's or a deleted one. */
  workflowName?: string | null;
  /** The first page of the workflow runs below this one, each right after the run that started it. */
  descendantRuns?: WorkflowRun[];
  /** How many workflow runs are below this one, on every page. */
  descendantRunCount?: number;
  /** Continues `descendantRuns`; `null` when they are all here. */
  descendantRunsCursor?: string | null;
  /** Whether any run below this one, on any page, is working or waiting for input. */
  descendantRunsActive?: boolean;
}

/** The automation whose scheduled run started a workflow run. */
export interface WorkflowRunAutomation {
  id: string;
  name: string;
}

/** The session that started a workflow run. */
export interface WorkflowRunStarter {
  sessionId: string;
  /** Its title; empty when it has none yet. */
  title: string;
}

/** The run is starting: its session waits on a sandbox that is still launching. */
export function isWorkflowRunLaunching(run: WorkflowRun): boolean {
  return run.isCurrent && run.sandboxLaunching === true;
}

/** The session's turn, delegated work, work left running between turns, or its sandbox launch. */
export function isWorkflowRunWorking(run: WorkflowRun): boolean {
  return (
    isWorkflowRunLaunching(run) ||
    (run.isCurrent && (run.sessionStatus === "running" || run.backgroundWorkRunning === true))
  );
}

/** Where each of the run's steps stands, as the Tasks tab works it out. */
export function runStepProgress(
  run: WorkflowRun,
  sessionWorking = isWorkflowRunWorking(run),
): StepProgress[] {
  return stepProgress(run.steps, run.statuses, run.isCurrent && sessionWorking);
}

/** How a run's row looks: its icon and its one-line state. */
export type WorkflowRunState =
  | { kind: "waiting"; label: string }
  | { kind: "done"; label: string }
  | { kind: "running"; label: string }
  | { kind: "failed"; label: string }
  | { kind: "stopped"; label: string };

/**
 * Where a run stands, for its row. A finished run comes first, then a question
 * for the user; a running session shows its progress; otherwise a failed
 * step or session says so, and a run that stopped partway shows its count.
 */
export function describeWorkflowRun(
  run: WorkflowRun,
  sessionWorking = isWorkflowRunWorking(run),
): WorkflowRunState {
  const { total, done, needsInput, failed } = run.progress;
  const count = `${done} of ${total} done`;
  const unknownActivePlan = run.planGraphRevision === undefined && run.isCurrent && sessionWorking;
  if (
    (run.planGraphRevision !== undefined &&
      isPlanComplete(runStepProgress(run, sessionWorking), run.planGraphRevision)) ||
    (!unknownActivePlan && total > 0 && done === total)
  )
    return { kind: "done", label: failed > 0 ? `Finished, ${failed} failed` : "Done" };
  if (needsInput > 0) return { kind: "waiting", label: "Waiting for you" };
  if (run.isCurrent && sessionWorking) return { kind: "running", label: count };
  if (failed > 0) return { kind: "failed", label: `Failed · ${count}` };
  if (run.isCurrent && run.sessionStatus === "failed")
    return { kind: "failed", label: `Session failed · ${count}` };
  return { kind: "stopped", label: count };
}
