/**
 * A workflow run's row, as a workflow's page and a session's Tasks tab list
 * it, with the workflow runs below it under "Ran N workflows".
 */

import { useMemo, useState } from "react";
import { ChevronRightIcon, ClockIcon, CornerLeftUpIcon } from "lucide-react";

import {
  RunGraphButton,
  RunPlanButton,
  RunTimeline,
  RunWorkflowButton,
  useRunTaskMenus,
} from "@/components/workflows/RunTasks";
import { Button } from "@/components/ui/button";
import { WorkflowGraphOverlay } from "@/components/workflows/WorkflowStepsGraph";
import { useConversationEntryValue } from "@/hooks/useConversationEntryState";
import { useAutomationsEnabled } from "@/hooks/useConversations";
import { useNow } from "@/hooks/useNow";
import { usePlanStopping } from "@/hooks/usePlanStopping";
import { useSessionWorking } from "@/hooks/useSessionWorking";
import { useWorkflowRunDescendants, useWorkflowRuns } from "@/hooks/useWorkflow";
import { automationPagePath } from "@/lib/automationFromSession";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { Link } from "@/lib/routing";
import { formatRunTimestamp } from "@/lib/scheduleText";
import type { StepState } from "@/lib/sessionJournal";
import { cn } from "@/lib/utils";
import {
  describeWorkflowRun,
  isWorkflowRunLaunching,
  isWorkflowRunWorking,
  runStepProgress,
  type WorkflowRun,
  type WorkflowRunAutomation,
  type WorkflowRunStarter,
  type WorkflowRunState,
} from "@/lib/workflowRuns";
import { planRunButtonState, planRunControl } from "@/shell/PlanRunButton";
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
  owned = true,
  parentSessionId,
}: {
  run: WorkflowRun;
  now: Date;
  /** The workflow whose runs show the row; its list refreshes after an action. */
  workflowId: string;
  /** The name of the run's workflow, or null when you can't see it. */
  workflowName: string | null;
  /**
   * Whether you own the run's workflow, and so the run: only then can a row
   * below another run open that workflow or act on the run.
   */
  owned?: boolean;
  /** For a run listed below another, that run's session, whose runs need no "Started by". */
  parentSessionId?: string;
}) {
  const sessionId = run.isCurrent ? run.sessionId : null;
  // A failed launch this page streams outranks the run list, which learns of it a refresh later.
  const launchFailed = useConversationEntryValue(
    sessionId,
    (state) => state.sandboxStatus?.stage === "failed",
  );
  const listed = launchFailed ? { ...run, sandboxLaunching: false } : run;
  // The sidebar's status for the session leaves out a sandbox launch, so it can't settle one.
  const sessionWorking =
    useSessionWorking(sessionId, isWorkflowRunWorking(listed)) || isWorkflowRunLaunching(listed);
  const state = describeWorkflowRun(run, sessionWorking);
  const progress = useMemo(() => runStepProgress(run, sessionWorking), [run, sessionWorking]);
  const nested = parentSessionId !== undefined;
  // Stop while the run's session works; play once it stopped with tasks left.
  const canAct = run.isCurrent && !run.archived && owned;
  const stopping = usePlanStopping(canAct ? run.sessionId : null, sessionWorking);
  const control = canAct
    ? planRunButtonState(
        planRunControl(progress, run.statuses, sessionWorking, run.planGraphRevision),
        stopping,
      )
    : null;
  // An action refreshes the list showing the row, and the run's own workflow's.
  const refreshKeys = [
    ["workflow-runs", workflowId],
    ["workflow-run-descendants", workflowId],
    ...(run.workflowId && run.workflowId !== workflowId ? [["workflow-runs", run.workflowId]] : []),
  ];
  const taskMenus = useRunTaskMenus(canAct ? run.sessionId : null, refreshKeys, run.id, stopping);
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
          {run.automation && <RunAutomation automation={run.automation} />}
          {startedBy && <RunStartedBy starter={startedBy} />}
          {progress.length > 0 && (
            <RunTimeline
              sessionId={run.sessionId}
              progress={progress}
              menuActionFor={taskMenus}
              className="mt-3 pr-1"
            />
          )}
          {!nested && !!run.descendantRuns?.length && (
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
        {nested && owned && run.workflowId && (
          <RunWorkflowButton workflowId={run.workflowId} className="relative -my-1" />
        )}
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

/** Links a run to the automation whose scheduled run started it. */
function RunAutomation({ automation }: { automation: WorkflowRunAutomation }) {
  const info = useServerInfo();
  const automationsEnabled = useAutomationsEnabled();
  if (!automationsEnabled) return null;
  return (
    <Link
      to={automationPagePath(info, automation)}
      className="relative z-10 mt-1 flex w-fit max-w-full items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
      data-testid="workflow-run-automation"
    >
      <ClockIcon aria-hidden className="size-3.5 shrink-0" />
      <span className="truncate">
        Scheduled · <span className="font-medium text-foreground">{automation.name}</span>
      </span>
    </Link>
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
  const [loadMore, setLoadMore] = useState(false);
  const more = useWorkflowRunDescendants(run.workflowId ?? workflowId, run, loadMore);
  // A later page can repeat a run when runs start between pages; list it once.
  const descendants = [
    ...new Map(
      [...(run.descendantRuns ?? []), ...(more.data?.pages.flatMap((page) => page.runs) ?? [])].map(
        (descendant) => [descendant.id, descendant],
      ),
    ).values(),
  ];
  const count = run.descendantRunCount ?? descendants.length;
  const hasMore = loadMore
    ? more.hasNextPage || more.isFetching || (more.isError && !more.data)
    : !!run.descendantRunsCursor;
  const active =
    isWorkflowRunWorking(run) ||
    run.descendantRunsActive === true ||
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
        Ran {count} {count === 1 ? "workflow" : "workflows"}
      </button>
      {shown && (
        <ul
          className="mt-1 flex flex-col gap-1 border-l-2 border-border pl-3"
          data-testid="workflow-run-descendants"
        >
          {descendants.map((descendant) => (
            // The server names a workflow only for its owner, who alone can open it.
            <WorkflowRunRow
              key={descendant.id}
              run={descendant}
              now={now}
              workflowId={workflowId}
              workflowName={descendant.workflowName ?? null}
              owned={descendant.workflowName != null}
              parentSessionId={run.sessionId}
            />
          ))}
          {hasMore && (
            <li>
              <Button
                variant="ghost"
                size="xs"
                className="text-xs text-muted-foreground"
                loading={more.isFetching}
                disabled={more.isFetching}
                onClick={() => {
                  if (!loadMore) setLoadMore(true);
                  else if (more.data) void more.fetchNextPage();
                  else void more.refetch();
                }}
                data-testid="workflow-run-descendants-more"
              >
                Show more
              </Button>
            </li>
          )}
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
  const run = listed.find((candidate) => candidate.isCurrent);
  if (!run?.descendantRuns?.length) return null;
  return (
    <section
      aria-label="Workflow runs"
      className="mt-2 border-t border-border px-2 pt-3"
      data-testid="tasks-descendant-runs"
    >
      <DescendantRuns key={run.id} run={run} now={now} workflowId={workflowId} />
    </section>
  );
}
