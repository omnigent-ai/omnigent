import {
  type JournalEntry,
  type JournalPlanStep,
  type StepProgress,
  stepProgress,
} from "@/lib/sessionJournal";

/** One run of a saved workflow: a session that started it. */
export interface WorkflowRun {
  /** The ID of the plan entry that started the run. */
  id: string;
  sessionId: string;
  /** The session's title; empty when it has none yet. */
  title: string;
  /** Unix epoch seconds when the run's plan was written. */
  startedAt: number;
  sessionStatus: "idle" | "running" | "failed";
  archived: boolean;
  /** The run's starting steps, counted by their current status. */
  progress: {
    total: number;
    started: number;
    done: number;
    needsInput: number;
    failed: number;
  };
  /** The steps of the plan that started the run. */
  steps: JournalPlanStep[];
  /** Each of those steps' newest status entry. */
  statuses: JournalEntry[];
}

/** Where each of the run's steps stands, as the Tasks tab works it out. */
export function runStepProgress(run: WorkflowRun): StepProgress[] {
  return stepProgress(run.steps, run.statuses, run.sessionStatus === "running");
}

/** How a run's row looks: its icon and its one-line state. */
export type WorkflowRunState =
  | { kind: "waiting"; label: string }
  | { kind: "done"; label: string }
  | { kind: "running"; label: string }
  | { kind: "failed"; label: string }
  | { kind: "stopped"; label: string };

/**
 * Where a run stands, for its row. A question for the user comes first, then
 * a finished run; a running session shows its progress; otherwise a failed
 * step or session says so, and a run that stopped partway shows its count.
 */
export function describeWorkflowRun(run: WorkflowRun): WorkflowRunState {
  const { total, done, needsInput, failed } = run.progress;
  const count = `${done} of ${total} done`;
  if (needsInput > 0) return { kind: "waiting", label: "Waiting for you" };
  if (total > 0 && done === total) return { kind: "done", label: "Done" };
  if (run.sessionStatus === "running") return { kind: "running", label: count };
  if (failed > 0) return { kind: "failed", label: `Failed · ${count}` };
  if (run.sessionStatus === "failed") return { kind: "failed", label: `Session failed · ${count}` };
  return { kind: "stopped", label: count };
}
