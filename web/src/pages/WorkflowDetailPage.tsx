/**
 * A workflow's plan, its planning conversation, and the sessions that ran it.
 * Drafts become runnable when the user saves the agent's proposed plan.
 */

import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDownIcon,
  ChevronLeftIcon,
  ClockIcon,
  Loader2Icon,
  PencilIcon,
  PlayIcon,
  SaveIcon,
  Trash2Icon,
  WorkflowIcon,
} from "lucide-react";

import { KeyboardShortcutTooltip } from "@/components/KeyboardShortcut";
import { PageScroll } from "@/components/PageScroll";
import { CreateScheduledTaskDialog } from "@/components/scheduled/CreateScheduledTaskDialog";
import { DeleteWorkflowDialog } from "@/components/workflows/DeleteWorkflowDialog";
import { WorkflowAutomations } from "@/components/workflows/WorkflowAutomations";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
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
import {
  useWorkflow,
  useWorkflowRuns,
  useSaveWorkflowDescription,
  useSaveWorkflowPlan,
  WorkflowNotFoundError,
} from "@/hooks/useWorkflow";
import { useSessionJournal } from "@/hooks/useSessionJournal";
import { useSessionWorking } from "@/hooks/useSessionWorking";
import { useAutomationsEnabled } from "@/hooks/useConversations";
import { useSession } from "@/hooks/useSession";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { WorkflowPlanningChat } from "@/components/workflows/WorkflowPlanningChat";
import { automationPagePath } from "@/lib/automationFromSession";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { Link, useNavigate, useParams } from "@/lib/routing";
import { formatRunTimestamp } from "@/lib/scheduleText";
import { cn } from "@/lib/utils";
import { currentPlan, planSteps, type StepState } from "@/lib/sessionJournal";
import { ApiError } from "@/lib/sessionsApi";
import {
  describeWorkflowRun,
  isWorkflowRunWorking,
  runStepProgress,
  type WorkflowRun,
  type WorkflowRunState,
} from "@/lib/workflowRuns";
import { StepGlyph } from "@/shell/TasksPanel";
import { useChatStore } from "@/store/chatStore";

// The planning chat's outline flash lasts as long as `.animate-user-msg-flash`.
const PLANNING_CHAT_FLASH_MS = 800;

export function WorkflowDetailPage() {
  const { workflowId = "" } = useParams<{ workflowId: string }>();
  // Remount per workflow, so dialogs and pages of runs don't carry over.
  return <WorkflowDetail key={workflowId} workflowId={workflowId} />;
}

function WorkflowDetail({ workflowId }: { workflowId: string }) {
  const navigate = useNavigate();
  const info = useServerInfo();
  const automationsEnabled = useAutomationsEnabled();
  const now = useNow();
  const { data: workflow, isLoading, isError, error, refetch } = useWorkflow(workflowId);
  const runs = useWorkflowRuns(workflowId);
  const [deleteOpen, setDeleteOpen] = useState(false);
  // The request stays set while its dialog closes, as on the Workflows page.
  const [sessionRequest, setSessionRequest] = useState<WorkflowSessionRequest | null>(null);
  const [sessionOpen, setSessionOpen] = useState(false);
  const [automationOpen, setAutomationOpen] = useState(false);
  const [plannerOpen, setPlannerOpen] = useState<boolean | null>(null);
  const [plannerExpanded, setPlannerExpanded] = useState(false);
  // Non-zero while the open planning chat flashes; each press restarts it.
  const [plannerFlash, setPlannerFlash] = useState(0);
  const showPlanner = plannerOpen ?? workflow?.is_draft ?? false;
  const isMobile = useIsMobileViewport();
  const { session: planningSession, error: planningSessionError } = useSession(
    workflow?.planning_session_id,
  );
  // Known archived or deleted; a session still loading may yet open.
  const planningSessionGone =
    !!planningSession?.archived ||
    (planningSessionError instanceof ApiError && planningSessionError.status === 404);
  const canReadProposal = !!planningSession && !planningSessionGone;
  const journal = useSessionJournal(
    canReadProposal ? planningSession.id : undefined,
    canReadProposal,
  );
  const savePlan = useSaveWorkflowPlan(workflowId);
  useEffect(() => {
    if (plannerFlash === 0) return;
    const timer = setTimeout(() => setPlannerFlash(0), PLANNING_CHAT_FLASH_MS);
    return () => clearTimeout(timer);
  }, [plannerFlash]);

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
  const proposal = canReadProposal ? currentPlan(journal.entries) : null;
  const proposedSteps = proposal ? planSteps(proposal) : null;
  const stepSignature = (steps: typeof workflow.steps) =>
    JSON.stringify(
      steps.map(({ id, name, goal, depends_on }) => [id, name, goal ?? null, depends_on]),
    );
  const hasChanges =
    !!proposedSteps?.length && stepSignature(proposedSteps) !== stepSignature(workflow.steps);
  const isDraft = workflow.is_draft ?? workflow.steps.length === 0;
  const canRun = !isDraft && !hasChanges;
  // Expanded, the planner covers the page, which keeps its width underneath so
  // it doesn't reflow; `invisible` keeps its scroll position and tab order out.
  const plannerFillsPage = showPlanner && !isMobile && plannerExpanded;
  // Every close path resets expansion, including the phone sheet after the window
  // narrows, so the planner reopens beside the page.
  const closePlanner = () => {
    setPlannerOpen(false);
    setPlannerExpanded(false);
  };
  // Opens the planning chat ready to type, or flashes it when it's already open.
  const openPlanner = () => {
    if (showPlanner) setPlannerFlash((count) => count + 1);
    setPlannerOpen(true);
    // A session still loading keeps the request until its composer mounts.
    const sessionId = workflow.planning_session_id;
    if (sessionId && !planningSessionGone) useChatStore.getState().requestSideChatFocus(sessionId);
  };

  return (
    <div className="relative flex h-full min-h-0 flex-1 overflow-hidden">
      <PageScroll
        className={cn("min-w-0", plannerFillsPage && "invisible")}
        contentClassName="px-6"
        data-testid="workflow-detail-page"
      >
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
            <KeyboardShortcutTooltip label="Edit plan with agent">
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label="Edit plan with agent"
                data-testid="workflow-detail-edit"
                onClick={openPlanner}
              >
                <PencilIcon className="size-4" />
              </Button>
            </KeyboardShortcutTooltip>
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="Delete workflow"
              data-testid="workflow-detail-delete"
              onClick={() => setDeleteOpen(true)}
            >
              <Trash2Icon className="size-4" />
            </Button>
            {automationsEnabled && (
              <Button
                variant="outline"
                data-testid="workflow-detail-create-automation"
                disabled={!canRun}
                onClick={() => setAutomationOpen(true)}
              >
                <ClockIcon className="size-4" />
                Create automation
              </Button>
            )}
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
        <WorkflowDescription workflowId={workflow.id} text={workflow.description} />

        <section className="mt-6">
          <div className="mb-3 flex items-center justify-between gap-3">
            <div>
              <h2 className="text-sm font-medium text-muted-foreground">Plan</h2>
              {(hasChanges || !isDraft) && (
                <p className="text-xs text-muted-foreground" role="status">
                  {hasChanges ? "Unsaved changes" : "Saved plan"}
                </p>
              )}
            </div>
            {hasChanges && (
              <Button
                size="sm"
                disabled={savePlan.isPending}
                loading={savePlan.isPending}
                onClick={() => {
                  if (!proposedSteps) return;
                  setPlannerOpen(showPlanner);
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
          {canReadProposal && journal.error && (
            <div role="alert" className="mb-3 flex items-center gap-2 text-sm text-destructive">
              <span>Couldn’t load the latest proposal.</span>
              <Button variant="outline" size="sm" onClick={journal.retry}>
                Retry
              </Button>
            </div>
          )}
          {(proposedSteps ?? workflow.steps).length > 0 ? (
            <WorkflowStepsGraph steps={proposedSteps ?? workflow.steps} title={workflow.name} />
          ) : (
            <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-border bg-card px-6 py-16 text-center">
              <WorkflowIcon className="size-8 text-muted-foreground/50" />
              <p className="text-ui font-medium">No plan yet</p>
              <p className="max-w-sm text-sm text-muted-foreground">
                Describe the workflow to an agent.
                <br />
                Your proposed steps will appear here.
              </p>
              {!showPlanner && (
                <Button variant="outline" size="sm" onClick={openPlanner}>
                  Create plan
                </Button>
              )}
            </div>
          )}
        </section>

        {automationsEnabled && !isDraft && (
          <WorkflowAutomations workflowId={workflowId} now={now} />
        )}

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

        {deleteOpen && (
          <DeleteWorkflowDialog
            workflow={workflow}
            onClose={() => setDeleteOpen(false)}
            onDeleted={() => navigate("/workflows")}
          />
        )}
        <WorkflowSessionDialog
          request={sessionRequest}
          open={sessionOpen}
          onOpenChange={setSessionOpen}
        />
        <CreateScheduledTaskDialog
          open={automationsEnabled && automationOpen}
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
          <Dialog
            open
            onOpenChange={(open) => {
              if (!open) closePlanner();
            }}
          >
            <DialogContent
              showCloseButton={false}
              aria-describedby={undefined}
              className="flex h-[90dvh] flex-col gap-0 overflow-hidden p-0"
            >
              <DialogTitle className="sr-only">
                {workflow.steps.length > 0 ? "Edit plan" : "Create plan"}
              </DialogTitle>
              <WorkflowPlanningChat workflow={workflow} onClose={closePlanner} />
            </DialogContent>
          </Dialog>
        ) : (
          // The aside keeps its width in the row while its panel covers the page.
          <aside aria-label="Workflow planning chat" className="w-[42%] min-w-96 max-w-xl shrink-0">
            <div
              data-testid="workflow-planner-panel"
              className={cn(
                "bg-background pt-[calc(var(--omnigent-header-height)+var(--omnigent-inset-top))] pb-[var(--omnigent-inset-bottom)]",
                plannerFillsPage ? "absolute inset-0" : "h-full border-l border-border",
              )}
            >
              <div className="relative h-full">
                <WorkflowPlanningChat
                  workflow={workflow}
                  expanded={plannerFillsPage}
                  onExpandedChange={setPlannerExpanded}
                  onClose={closePlanner}
                />
                {plannerFlash > 0 && (
                  <span
                    key={plannerFlash}
                    aria-hidden="true"
                    data-testid="workflow-planning-chat-flash"
                    className="pointer-events-none absolute inset-1 z-10 animate-user-msg-flash"
                  />
                )}
              </div>
            </div>
          </aside>
        ))}
    </div>
  );
}

/**
 * The description, in a section like an automation's prompt: three lines and
 * an ellipsis, with a chevron to show the rest when there is more. Its pencil
 * edits it in place, like a pull request's description.
 */
function WorkflowDescription({ workflowId, text }: { workflowId: string; text: string }) {
  const [expanded, setExpanded] = useState(false);
  const [clamped, setClamped] = useState(false);
  // The text being edited; null while the description is shown.
  const [draft, setDraft] = useState<string | null>(null);
  const editing = draft !== null;
  const bodyRef = useRef<HTMLParagraphElement>(null);
  const editorRef = useRef<HTMLFormElement>(null);
  const errorId = useId();
  const update = useSaveWorkflowDescription(workflowId);

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
  }, [text, expanded, editing]);

  // The editor opens ready to continue the text, with the caret after it.
  useEffect(() => {
    const input = editorRef.current?.querySelector("textarea");
    if (!editing || !input) return;
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }, [editing]);

  const close = () => {
    setDraft(null);
    update.reset();
  };
  const save = () => {
    if (draft === null) return;
    const next = draft.trim();
    // The shortcut follows the Save button: a workflow needs a description.
    if (!next) return;
    if (next === text) {
      close();
      return;
    }
    update.mutate(next, { onSuccess: close });
  };
  const cancel = () => {
    const changed = draft !== null && draft.trim() !== text;
    if (changed && !window.confirm("Discard your description changes?")) return;
    close();
  };

  return (
    <section className="mt-6">
      <div className="mb-2 flex items-center gap-1">
        <h2 className="text-sm font-medium text-muted-foreground">
          {!editing && (clamped || expanded) ? (
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
        {!editing && (
          <KeyboardShortcutTooltip label="Edit description">
            <Button
              variant="ghost"
              size="icon-xs"
              aria-label="Edit description"
              className="-my-0.5 text-muted-foreground"
              onClick={() => setDraft(text)}
            >
              <PencilIcon aria-hidden="true" className="size-3" />
            </Button>
          </KeyboardShortcutTooltip>
        )}
      </div>
      {editing ? (
        <form
          ref={editorRef}
          className="flex flex-col gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            save();
          }}
        >
          <Textarea
            aria-label="Workflow description"
            aria-invalid={update.isError || undefined}
            aria-describedby={update.isError ? errorId : undefined}
            value={draft}
            maxLength={2000}
            rows={4}
            disabled={update.isPending}
            onChange={(event) => setDraft(event.currentTarget.value)}
            onKeyDown={(event) => {
              // These keys end an IME composition first; leave them to the IME.
              if (isImeCompositionKeyEvent(event)) return;
              if (event.key === "Escape") {
                event.preventDefault();
                cancel();
              } else if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                event.preventDefault();
                save();
              }
            }}
            className="min-h-24 resize-y text-sm"
          />
          {update.isError && (
            <p id={errorId} role="alert" className="text-sm text-destructive">
              Couldn’t save the description. {update.error.message}
            </p>
          )}
          <div className="flex items-center gap-2">
            <Button
              type="submit"
              size="sm"
              loading={update.isPending}
              disabled={update.isPending || !draft.trim()}
            >
              Save
            </Button>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              disabled={update.isPending}
              onClick={cancel}
            >
              Cancel
            </Button>
          </div>
        </form>
      ) : (
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
      )}
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
            runId={run.id}
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
