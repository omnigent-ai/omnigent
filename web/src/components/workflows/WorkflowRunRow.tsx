/**
 * A workflow run's row, as a workflow's page and a session's Tasks tab list
 * it, with the workflow runs below it under "Ran N workflows".
 */

import { useMemo, useState } from "react";
import { ChevronRightIcon, CornerLeftUpIcon } from "lucide-react";

import {
  RunGraphButton,
  RunPlanButton,
  RunTimeline,
  RunWorkflowButton,
  useRunTaskMenus,
} from "@/components/workflows/RunTasks";
import { WorkflowGraphOverlay } from "@/components/workflows/WorkflowStepsGraph";
import { useNow } from "@/hooks/useNow";
import { useSavedWorkflows } from "@/hooks/useSavedWorkflows";
import { useSessionWorking } from "@/hooks/useSessionWorking";
import { useWorkflowRuns } from "@/hooks/useWorkflow";
import { Link } from "@/lib/routing";
import { formatRunTimestamp } from "@/lib/scheduleText";
import type { StepState } from "@/lib/sessionJournal";
import { cn } from "@/lib/utils";
import {
  describeWorkflowRun,
  isWorkflowRunWorking,
  runStepProgress,
  type WorkflowRun,
  type WorkflowRunStarter,
  type WorkflowRunState,
} from "@/lib/workflowRuns";
import { planRunControl } from "@/shell/PlanRunButton";
import { StepGlyph } from "@/shell/TasksPanel";

// A run's glyph: the Tasks tab's, for the step state its run state matches.
const RUN_GLYPHS: Record<WorkflowRunState["kind"], StepState> = {
  running: "running",
  waiting: "needs_input",
  done: "done",
  failed: "failed",
  stopped: "stopped",
};

/**
 * One run: its session's title, when it started and where it stands, over the
 * Tasks tab's timeline of its tasks. The row opens the session; a segment opens
 * that task there, and the graph button shows the run on the workflow's graph.
 */
export function WorkflowRunRow({
  run,
  now,
  workflowId,
  workflowName,
  openWorkflow = false,
  parentSessionId,
}: {
  run: WorkflowRun;
  now: Date;
  /** The workflow whose runs show the row; its list refreshes after an action. */
  workflowId: string;
  /** The name of the run's workflow, or null when you can't see it. */
  workflowName: string | null;
  /** Whether the row offers to open the run's workflow, which only its owner can. */
  openWorkflow?: boolean;
  /** For a run listed below another, that run's session, whose runs need no "Started by". */
  parentSessionId?: string;
}) {
  const sessionWorking = useSessionWorking(
    run.isCurrent ? run.sessionId : null,
    isWorkflowRunWorking(run),
  );
  const state = describeWorkflowRun(run, sessionWorking);
  const progress = useMemo(() => runStepProgress(run, sessionWorking), [run, sessionWorking]);
  // Stop while the run's session works; play once it stopped with tasks left.
  const canAct = run.isCurrent && !run.archived;
  const control = canAct
    ? planRunControl(progress, run.statuses, sessionWorking, run.planGraphRevision)
    : null;
  const refreshKeys = [["workflow-runs", workflowId]];
  const taskMenus = useRunTaskMenus(canAct ? run.sessionId : null, refreshKeys, run.id);
  const [graphOpen, setGraphOpen] = useState(false);
  const sessionPath = `/c/${encodeURIComponent(run.sessionId)}`;
  const title = run.title || "Untitled session";
  const started = formatRunTimestamp(run.startedAt, now) ?? "—";
  const startedBy = run.startedBy?.sessionId !== parentSessionId ? run.startedBy : undefined;
  const stateLabel = (
    <span
      className={cn(state.kind === "waiting" && "text-brand-accent")}
      data-testid="workflow-run-state"
    >
      {state.label}
    </span>
  );
  return (
    <li
      className="relative rounded-lg px-2 py-2.5 transition-colors hover:bg-muted/60 has-[a:focus-visible]:bg-muted/60"
      data-testid="workflow-run"
      data-run-state={state.kind}
    >
      <div className="flex items-start gap-3">
        <span className="flex size-5 shrink-0 items-center justify-center">
          <StepGlyph state={RUN_GLYPHS[state.kind]} />
        </span>
        <div className="min-w-0 flex-1">
          {/* The link covers the row; the timeline and the graph button sit above it. */}
          <Link
            to={sessionPath}
            className="block truncate text-sm leading-5 after:absolute after:inset-0 after:rounded-lg after:content-[''] focus-visible:outline-none"
          >
            {title}
          </Link>
          <p className="mt-0.5 flex flex-wrap items-baseline gap-x-2 text-xs text-muted-foreground">
            <span>{started}</span>
            {stateLabel}
            {run.archived && <span>Archived</span>}
          </p>
          {startedBy && <RunStartedBy starter={startedBy} />}
          {progress.length > 0 && (
            <RunTimeline
              sessionId={run.sessionId}
              progress={progress}
              menuActionFor={taskMenus}
              className="mt-3 pr-1"
            />
          )}
          {parentSessionId === undefined && !!run.descendantRuns?.length && (
            <DescendantRuns run={run} now={now} workflowId={workflowId} className="mt-3" />
          )}
        </div>
        {control && (
          <RunPlanButton
            sessionId={run.sessionId}
            runId={run.id}
            planName={workflowName}
            control={control}
            refreshKeys={refreshKeys}
            className="relative -my-1"
          />
        )}
        {openWorkflow && run.workflowId && <RunWorkflowButton workflowId={run.workflowId} />}
        <RunGraphButton
          runName={title}
          className="relative -my-1"
          onClick={() => setGraphOpen(true)}
        />
      </div>
      {graphOpen && (
        <WorkflowGraphOverlay
          title={workflowName ?? title}
          subtitle={
            <>
              <Link to={sessionPath} className="text-foreground hover:underline">
                {title}
              </Link>
              <span aria-hidden>·</span>
              <span>{started}</span>
              <span aria-hidden>·</span>
              {stateLabel}
            </>
          }
          steps={run.steps}
          progress={progress}
          menuActionFor={taskMenus}
          onClose={() => setGraphOpen(false)}
        />
      )}
    </li>
  );
}

/** Links a run to the session that started it. */
function RunStartedBy({ starter }: { starter: WorkflowRunStarter }) {
  return (
    <Link
      to={`/c/${encodeURIComponent(starter.sessionId)}`}
      className="relative z-10 mt-1 flex w-fit max-w-full items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
      data-testid="workflow-run-started-by"
    >
      <CornerLeftUpIcon aria-hidden className="size-3.5 shrink-0" />
      <span className="truncate">
        Started by{" "}
        <span className="font-medium text-foreground">{starter.title || "Untitled session"}</span>
      </span>
    </Link>
  );
}

/**
 * "Ran N workflows": every workflow run below *run*, each right after the run
 * that started it. The list starts open while any of them is working or waiting.
 * Mount it only for a run with runs below it.
 */
export function DescendantRuns({
  run,
  now,
  workflowId,
  className,
}: {
  run: WorkflowRun;
  now: Date;
  /** The workflow whose runs list shows *run*. */
  workflowId: string;
  className?: string;
}) {
  const [open, setOpen] = useState<boolean | null>(null);
  // Runs below this one can follow other workflows; only their owner can open them.
  const { workflows } = useSavedWorkflows();
  const descendants = run.descendantRuns ?? [];
  const active =
    isWorkflowRunWorking(run) ||
    descendants.some(
      (descendant) => isWorkflowRunWorking(descendant) || descendant.progress.needsInput > 0,
    );
  const shown = open ?? active;
  return (
    <div className={cn("relative z-10", className)}>
      <button
        type="button"
        aria-expanded={shown}
        onClick={() => setOpen(!shown)}
        className="flex cursor-pointer items-center gap-1 rounded-sm text-xs text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        data-testid="workflow-run-descendants-toggle"
      >
        <ChevronRightIcon
          aria-hidden
          className={cn("size-3.5 transition-transform", shown && "rotate-90")}
        />
        Ran {descendants.length} {descendants.length === 1 ? "workflow" : "workflows"}
      </button>
      {shown && (
        <ul
          className="mt-1 flex flex-col gap-1 border-l-2 border-border pl-3"
          data-testid="workflow-run-descendants"
        >
          {descendants.map((descendant) => {
            const own = workflows.find((workflow) => workflow.id === descendant.workflowId);
            return (
              <WorkflowRunRow
                key={descendant.id}
                run={descendant}
                now={now}
                workflowId={workflowId}
                workflowName={own?.name ?? null}
                openWorkflow={own !== undefined}
                parentSessionId={run.sessionId}
              />
            );
          })}
        </ul>
      )}
    </div>
  );
}

/**
 * In a session's Tasks tab, the workflow runs below its current run of
 * *workflowId*; nothing when there are none.
 */
export function SessionDescendantRuns({
  sessionId,
  workflowId,
}: {
  sessionId: string;
  workflowId: string;
}) {
  const runs = useWorkflowRuns(workflowId, sessionId);
  const now = useNow();
  const listed = runs.data?.pages[0]?.runs ?? [];
  const run = listed.find((candidate) => candidate.isCurrent) ?? listed[0];
  if (!run?.descendantRuns?.length) return null;
  return (
    <section
      aria-label="Workflow runs"
      className="mt-2 border-t border-border px-2 pt-3"
      data-testid="tasks-descendant-runs"
    >
      <DescendantRuns run={run} now={now} workflowId={workflowId} />
    </section>
  );
}
