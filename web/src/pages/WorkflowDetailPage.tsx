/**
 * A workflow's plan, its planning conversation, and the sessions that ran it.
 * Drafts become runnable when the user saves the agent's proposed plan.
 */

import { useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDownIcon,
  ChevronLeftIcon,
  ClockIcon,
  Loader2Icon,
  MessageSquareIcon,
  PlayIcon,
  SaveIcon,
  Trash2Icon,
  WorkflowIcon,
} from "lucide-react";

import { PageScroll } from "@/components/PageScroll";
import { CreateScheduledTaskDialog } from "@/components/scheduled/CreateScheduledTaskDialog";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { showToast } from "@/components/ui/toast";
import {
  type WorkflowSessionRequest,
  WorkflowSessionDialog,
} from "@/components/workflows/WorkflowSessionDialog";
import {
  RunGraphButton,
  RunPlanButton,
  RunTimeline,
  useRunTaskMenus,
} from "@/components/workflows/RunTasks";
import { planRunControl } from "@/shell/PlanRunButton";
import {
  WorkflowGraphOverlay,
  WorkflowStepsGraph,
} from "@/components/workflows/WorkflowStepsGraph";
import { useNow } from "@/hooks/useNow";
import { useDeleteWorkflow } from "@/hooks/useSavedWorkflows";
import {
  useWorkflow,
  useWorkflowRuns,
  useSaveWorkflowPlan,
  WorkflowNotFoundError,
} from "@/hooks/useWorkflow";
import { useSessionJournal } from "@/hooks/useSessionJournal";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { WorkflowPlanningChat } from "@/components/workflows/WorkflowPlanningChat";
import { automationPagePath } from "@/lib/automationFromSession";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { Link, useNavigate, useParams } from "@/lib/routing";
import { formatRunTimestamp } from "@/lib/scheduleText";
import { cn } from "@/lib/utils";
import { currentPlan, planSteps, type StepState } from "@/lib/sessionJournal";
import {
  describeWorkflowRun,
  runStepProgress,
  type WorkflowRun,
  type WorkflowRunState,
} from "@/lib/workflowRuns";
import { StepGlyph } from "@/shell/TasksPanel";

export function WorkflowDetailPage() {
  const { workflowId = "" } = useParams<{ workflowId: string }>();
  // Remount per workflow, so dialogs and pages of runs don't carry over.
  return <WorkflowDetail key={workflowId} workflowId={workflowId} />;
}

function WorkflowDetail({ workflowId }: { workflowId: string }) {
  const navigate = useNavigate();
  const info = useServerInfo();
  const now = useNow();
  const { data: workflow, isLoading, isError, error, refetch } = useWorkflow(workflowId);
  const runs = useWorkflowRuns(workflowId);
  const deleteWorkflow = useDeleteWorkflow();
  // The request stays set while its dialog closes, as on the Workflows page.
  const [sessionRequest, setSessionRequest] = useState<WorkflowSessionRequest | null>(null);
  const [sessionOpen, setSessionOpen] = useState(false);
  const [automationOpen, setAutomationOpen] = useState(false);
  const [plannerOpen, setPlannerOpen] = useState<boolean | null>(null);
  const showPlanner = plannerOpen ?? workflow?.is_draft ?? false;
  const isMobile = useIsMobileViewport();
  const journal = useSessionJournal(workflow?.planning_session_id ?? undefined, showPlanner);
  const savePlan = useSaveWorkflowPlan(workflowId);

  if (isLoading) {
    return (
      <PageScroll contentClassName="px-6" data-testid="workflow-detail-loading">
        <div className="flex items-center gap-2 py-12 text-sm text-muted-foreground">
          <Loader2Icon className="size-4 animate-spin" />
          Loading workflow…
        </div>
      </PageScroll>
    );
  }

  // A failed request can be retried; only a 404 means the workflow is gone.
  if (isError && !(error instanceof WorkflowNotFoundError)) {
    return (
      <PageScroll contentClassName="px-6" data-testid="workflow-detail-load-error">
        <BackLink />
        <div className="flex flex-col items-start gap-2 py-12">
          <p className="text-sm font-medium">Couldn’t load this workflow.</p>
          <Button variant="outline" size="sm" onClick={() => void refetch()}>
            Try again
          </Button>
        </div>
      </PageScroll>
    );
  }

  if (isError || !workflow) {
    return (
      <PageScroll contentClassName="px-6" data-testid="workflow-detail-not-found">
        <BackLink />
        <div className="flex flex-col items-start gap-2 py-12">
          <p className="text-sm font-medium">This workflow couldn’t be found.</p>
          <p className="text-sm text-muted-foreground">
            It may have been deleted, or the link is out of date.
          </p>
        </div>
      </PageScroll>
    );
  }

  const summary = { id: workflow.id, name: workflow.name, description: workflow.description };
  const runList = runs.data?.pages.flatMap((page) => page.runs) ?? [];
  const proposal = showPlanner ? currentPlan(journal.entries) : null;
  const proposedSteps = proposal ? planSteps(proposal) : null;
  const stepSignature = (steps: typeof workflow.steps) =>
    JSON.stringify(
      steps.map(({ id, name, goal, depends_on }) => [id, name, goal ?? null, depends_on]),
    );
  const hasChanges =
    !!proposedSteps?.length && stepSignature(proposedSteps) !== stepSignature(workflow.steps);
  const isDraft = workflow.is_draft ?? workflow.steps.length === 0;
  const canRun = !isDraft && !hasChanges;

  return (
    <div className="relative flex h-full min-h-0 flex-1 overflow-hidden">
      <PageScroll className="min-w-0" contentClassName="px-6" data-testid="workflow-detail-page">
        <BackLink />

        <div className="mt-4 flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <h1
              className="min-w-0 break-words text-2xl font-semibold"
              data-testid="workflow-detail-title"
            >
              {workflow.name}
            </h1>
            {isDraft && (
              <span className="mt-2 inline-flex rounded-full bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground">
                Draft
              </span>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="outline"
              onClick={() => setPlannerOpen(!showPlanner)}
              aria-expanded={showPlanner}
            >
              <MessageSquareIcon className="size-4" />
              Plan with agent
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="Delete workflow"
              data-testid="workflow-detail-delete"
              disabled={deleteWorkflow.isPending}
              onClick={() =>
                deleteWorkflow.mutate(workflow.id, {
                  onSuccess: () => navigate("/workflows"),
                  onError: () => showToast(`Couldn’t delete “${workflow.name}”.`),
                })
              }
            >
              <Trash2Icon className="size-4" />
            </Button>
            <Button
              variant="outline"
              data-testid="workflow-detail-create-automation"
              disabled={!canRun}
              onClick={() => setAutomationOpen(true)}
            >
              <ClockIcon className="size-4" />
              Create automation
            </Button>
            <Button
              data-testid="workflow-detail-run"
              disabled={!canRun}
              onClick={() => {
                setSessionRequest({ kind: "run", workflow: summary });
                setSessionOpen(true);
              }}
            >
              <PlayIcon className="size-4" />
              Run
            </Button>
          </div>
        </div>
        <WorkflowDescription text={workflow.description} />

        <section className="mt-6">
          <div className="mb-3 flex items-center justify-between gap-3">
            <div>
              <h2 className="text-sm font-medium">Plan</h2>
              <p className="text-xs text-muted-foreground" role="status">
                {hasChanges
                  ? "Unsaved proposal · Save to enable Run"
                  : isDraft
                    ? "No saved plan yet"
                    : "Saved plan"}
              </p>
            </div>
            {hasChanges && (
              <Button
                size="sm"
                disabled={savePlan.isPending}
                loading={savePlan.isPending}
                onClick={() => {
                  if (!proposedSteps) return;
                  setPlannerOpen(true);
                  savePlan.mutate(proposedSteps, { onSuccess: () => showToast("Plan saved") });
                }}
              >
                <SaveIcon className="size-4" /> Save plan
              </Button>
            )}
          </div>
          {savePlan.isError && (
            <p role="alert" className="mb-3 text-sm text-destructive">
              Couldn’t save the plan. {savePlan.error.message}
            </p>
          )}
          {showPlanner && journal.error && (
            <p role="alert" className="mb-3 text-sm text-destructive">
              Couldn’t load the latest proposal. Reopen the chat to try again.
            </p>
          )}
          {(proposedSteps ?? workflow.steps).length > 0 ? (
            <WorkflowStepsGraph steps={proposedSteps ?? workflow.steps} title={workflow.name} />
          ) : (
            <div className="flex min-h-64 flex-col items-center justify-center gap-3 rounded-xl border border-dashed border-border px-6 text-center">
              <WorkflowIcon className="size-8 text-muted-foreground/50" />
              <h3 className="text-sm font-medium">Let’s shape the plan</h3>
              <p className="max-w-sm text-sm text-muted-foreground">
                Describe the outcome in chat. Your agent’s proposed steps will appear here as you
                work together.
              </p>
              {!showPlanner && (
                <Button variant="outline" size="sm" onClick={() => setPlannerOpen(true)}>
                  Plan with agent
                </Button>
              )}
            </div>
          )}
        </section>

        {!isDraft && (
          <>
            <hr className="my-6 border-border" />

            <h2 className="mb-2 text-sm font-medium text-muted-foreground">Runs</h2>
            {runs.isLoading ? (
              <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
                <Loader2Icon className="size-4 animate-spin" />
                Loading runs…
              </div>
            ) : runs.isError ? (
              <p className="pb-2 text-sm text-muted-foreground" data-testid="workflow-runs-error">
                Couldn’t load runs.
              </p>
            ) : runList.length === 0 && !runs.hasNextPage ? (
              <p className="pb-2 text-sm text-muted-foreground" data-testid="workflow-runs-empty">
                No runs yet.
              </p>
            ) : (
              <>
                <ul className="flex flex-col gap-1" data-testid="workflow-runs">
                  {runList.map((run) => (
                    <WorkflowRunRow
                      key={run.id}
                      run={run}
                      now={now}
                      workflowId={workflowId}
                      workflowName={workflow.name}
                    />
                  ))}
                </ul>
                {runs.hasNextPage && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="mt-2"
                    data-testid="workflow-runs-more"
                    disabled={runs.isFetchingNextPage}
                    onClick={() => void runs.fetchNextPage()}
                  >
                    {runs.isFetchingNextPage ? "Loading…" : "Load more"}
                  </Button>
                )}
              </>
            )}
          </>
        )}

        <WorkflowSessionDialog
          request={sessionRequest}
          open={sessionOpen}
          onOpenChange={setSessionOpen}
        />
        <CreateScheduledTaskDialog
          open={automationOpen}
          onOpenChange={setAutomationOpen}
          initialName={workflow.name}
          lockedWorkflow={summary}
          onCreated={(task) => {
            showToast("Automation created");
            navigate(automationPagePath(info, task));
          }}
        />
      </PageScroll>
      {showPlanner &&
        (isMobile ? (
          <Dialog open onOpenChange={setPlannerOpen}>
            <DialogContent
              showCloseButton={false}
              aria-describedby={undefined}
              className="flex h-[90dvh] flex-col gap-0 overflow-hidden p-0"
            >
              <DialogTitle className="sr-only">Plan with agent</DialogTitle>
              <WorkflowPlanningChat workflow={workflow} onClose={() => setPlannerOpen(false)} />
            </DialogContent>
          </Dialog>
        ) : (
          <aside
            aria-label="Workflow planning chat"
            className="w-[42%] min-w-96 max-w-xl shrink-0 border-l border-border bg-background pt-[calc(var(--omnigent-header-height)+var(--omnigent-inset-top))] pb-[var(--omnigent-inset-bottom)]"
          >
            <WorkflowPlanningChat workflow={workflow} onClose={() => setPlannerOpen(false)} />
          </aside>
        ))}
    </div>
  );
}

/**
 * The description, in a section like an automation's prompt: three lines and
 * an ellipsis, with a chevron to show the rest when there is more.
 */
function WorkflowDescription({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const [clamped, setClamped] = useState(false);
  const bodyRef = useRef<HTMLParagraphElement>(null);

  // Whether three lines cut the text off, measured again as the page resizes.
  useLayoutEffect(() => {
    const body = bodyRef.current;
    if (!body || expanded) return;
    const measure = () => setClamped(body.scrollHeight > body.clientHeight + 1);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(body);
    return () => observer.disconnect();
  }, [text, expanded]);

  return (
    <section className="mt-6">
      <h2 className="mb-2 text-sm font-medium text-muted-foreground">
        {clamped || expanded ? (
          <button
            type="button"
            aria-expanded={expanded}
            data-testid="workflow-detail-description-toggle"
            onClick={() => setExpanded(!expanded)}
            className="flex cursor-pointer items-center gap-1 rounded-sm text-left transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <ChevronDownIcon
              aria-hidden="true"
              className={cn("size-4 transition-transform", !expanded && "-rotate-90")}
            />
            Description
          </button>
        ) : (
          "Description"
        )}
      </h2>
      <p
        ref={bodyRef}
        className={cn(
          "whitespace-pre-wrap text-sm text-foreground/90",
          !expanded && "line-clamp-3",
        )}
        data-testid="workflow-detail-description"
      >
        {text}
      </p>
    </section>
  );
}

function BackLink() {
  return (
    <Link
      to="/workflows"
      data-testid="workflow-detail-back"
      className="inline-flex items-center gap-1 text-sm text-muted-foreground transition-colors hover:text-foreground"
    >
      <ChevronLeftIcon className="size-4" />
      Workflows
    </Link>
  );
}

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
function WorkflowRunRow({
  run,
  now,
  workflowId,
  workflowName,
}: {
  run: WorkflowRun;
  now: Date;
  workflowId: string;
  workflowName: string;
}) {
  const state = describeWorkflowRun(run);
  const progress = useMemo(() => runStepProgress(run), [run]);
  // Stop while the run's session works; play once it stopped with tasks left.
  const control = run.archived
    ? null
    : planRunControl(progress, run.statuses, run.sessionStatus === "running");
  const refreshKeys = [["workflow-runs", workflowId]];
  // Its tasks' ⋯ menus, on the timeline's cards and the graph's nodes; an archived run has none.
  const taskMenus = useRunTaskMenus(run.archived ? null : run.sessionId, refreshKeys);
  const [graphOpen, setGraphOpen] = useState(false);
  const sessionPath = `/c/${encodeURIComponent(run.sessionId)}`;
  const title = run.title || "Untitled session";
  const started = formatRunTimestamp(run.startedAt, now) ?? "—";
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
          {progress.length > 0 && (
            <RunTimeline
              sessionId={run.sessionId}
              progress={progress}
              menuActionFor={taskMenus}
              className="mt-3 pr-1"
            />
          )}
        </div>
        {control && (
          <RunPlanButton
            sessionId={run.sessionId}
            planName={workflowName}
            control={control}
            refreshKeys={refreshKeys}
            className="relative -my-1"
          />
        )}
        <RunGraphButton
          runName={title}
          className="relative -my-1"
          onClick={() => setGraphOpen(true)}
        />
      </div>
      {graphOpen && (
        <WorkflowGraphOverlay
          title={workflowName}
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
