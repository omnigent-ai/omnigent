/**
 * A workflow's plan, its planning conversation, and the sessions that ran it.
 * Drafts become runnable when the user saves the agent's proposed plan.
 */

import { useLayoutEffect, useRef, useState } from "react";
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

import { PageScroll } from "@/components/PageScroll";
import { CreateScheduledTaskDialog } from "@/components/scheduled/CreateScheduledTaskDialog";
import { DeleteWorkflowDialog } from "@/components/workflows/DeleteWorkflowDialog";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { showToast } from "@/components/ui/toast";
import {
  type WorkflowSessionRequest,
  WorkflowSessionDialog,
} from "@/components/workflows/WorkflowSessionDialog";
import { WorkflowRunRow } from "@/components/workflows/WorkflowRunRow";
import { WorkflowStepsGraph } from "@/components/workflows/WorkflowStepsGraph";
import { useNow } from "@/hooks/useNow";
import {
  useWorkflow,
  useWorkflowRuns,
  useSaveWorkflowPlan,
  WorkflowNotFoundError,
} from "@/hooks/useWorkflow";
import { useSessionJournal } from "@/hooks/useSessionJournal";
import { useAutomationsEnabled } from "@/hooks/useConversations";
import { useSession } from "@/hooks/useSession";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { WorkflowPlanningChat } from "@/components/workflows/WorkflowPlanningChat";
import { automationPagePath } from "@/lib/automationFromSession";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { Link, useNavigate, useParams } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { currentPlan, planSteps } from "@/lib/sessionJournal";
import { ApiError } from "@/lib/sessionsApi";

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
  const showPlanner = plannerOpen ?? workflow?.is_draft ?? false;
  const isMobile = useIsMobileViewport();
  const { session: planningSession, error: planningSessionError } = useSession(
    workflow?.planning_session_id,
  );
  const canReadProposal =
    !!planningSession &&
    !planningSession.archived &&
    !(planningSessionError instanceof ApiError && planningSessionError.status === 404);
  const journal = useSessionJournal(
    canReadProposal ? planningSession.id : undefined,
    canReadProposal,
  );
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
              variant="ghost"
              size="icon-sm"
              aria-label="Edit workflow"
              data-testid="workflow-detail-edit"
              onClick={() => setPlannerOpen(true)}
            >
              <PencilIcon className="size-4" />
            </Button>
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
        <WorkflowDescription text={workflow.description} />

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
                <Button variant="outline" size="sm" onClick={() => setPlannerOpen(true)}>
                  Create plan
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
          <Dialog open onOpenChange={setPlannerOpen}>
            <DialogContent
              showCloseButton={false}
              aria-describedby={undefined}
              className="flex h-[90dvh] flex-col gap-0 overflow-hidden p-0"
            >
              <DialogTitle className="sr-only">
                {workflow.steps.length > 0 ? "Edit plan" : "Create plan"}
              </DialogTitle>
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
