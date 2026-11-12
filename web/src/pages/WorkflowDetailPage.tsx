/**
 * A saved workflow's page (`/workflows/:workflowId`), laid out like an
 * automation's: its name and description, Run, Create automation and Delete,
 * its steps as a graph, and the sessions that ran it, newest first.
 */

import { useState } from "react";
import {
  CheckIcon,
  ChevronLeftIcon,
  ClockIcon,
  Loader2Icon,
  PlayIcon,
  Trash2Icon,
  TriangleAlertIcon,
} from "lucide-react";

import { PageScroll } from "@/components/PageScroll";
import { CreateScheduledTaskDialog } from "@/components/scheduled/CreateScheduledTaskDialog";
import { Button } from "@/components/ui/button";
import { showToast } from "@/components/ui/toast";
import {
  type WorkflowSessionRequest,
  WorkflowSessionDialog,
} from "@/components/workflows/WorkflowSessionDialog";
import { WorkflowStepsGraph } from "@/components/workflows/WorkflowStepsGraph";
import { useNow } from "@/hooks/useNow";
import { useDeleteWorkflow } from "@/hooks/useSavedWorkflows";
import { useWorkflow, useWorkflowRuns, WorkflowNotFoundError } from "@/hooks/useWorkflow";
import { automationPagePath } from "@/lib/automationFromSession";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { Link, useNavigate, useParams } from "@/lib/routing";
import { formatRunTimestamp } from "@/lib/scheduleText";
import { cn } from "@/lib/utils";
import { describeWorkflowRun, type WorkflowRun, type WorkflowRunState } from "@/lib/workflowRuns";

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

  return (
    <PageScroll contentClassName="px-6" data-testid="workflow-detail-page">
      <BackLink />

      <div className="mt-4 flex items-start justify-between gap-4">
        <h1
          className="min-w-0 break-words text-2xl font-semibold"
          data-testid="workflow-detail-title"
        >
          {workflow.name}
        </h1>
        <div className="flex shrink-0 items-center gap-2">
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
            onClick={() => setAutomationOpen(true)}
          >
            <ClockIcon className="size-4" />
            Create automation
          </Button>
          <Button
            data-testid="workflow-detail-run"
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
      <p
        className="mt-2 whitespace-pre-wrap text-sm text-muted-foreground"
        data-testid="workflow-detail-description"
      >
        {workflow.description}
      </p>

      <section className="mt-6">
        <h2 className="mb-2 text-sm font-medium text-muted-foreground">Steps</h2>
        <WorkflowStepsGraph steps={workflow.steps} />
      </section>

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
              <WorkflowRunRow key={run.id} run={run} now={now} />
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

/** One run: its session's title, when it started and where it stands, opening the session. */
function WorkflowRunRow({ run, now }: { run: WorkflowRun; now: Date }) {
  const state = describeWorkflowRun(run);
  return (
    <li data-testid="workflow-run" data-run-state={state.kind}>
      <Link
        to={`/c/${encodeURIComponent(run.sessionId)}`}
        className="flex items-center gap-3 rounded-lg px-2 py-2 transition-colors hover:bg-muted focus-visible:bg-muted focus-visible:outline-none"
      >
        <span className="flex size-5 shrink-0 items-center justify-center">
          <RunStateIcon state={state} />
        </span>
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="truncate text-sm">{run.title || "Untitled session"}</span>
          <span className="flex flex-wrap items-baseline gap-x-2 text-xs text-muted-foreground">
            <span>{formatRunTimestamp(run.startedAt, now) ?? "—"}</span>
            <span
              className={cn(state.kind === "waiting" && "text-amber-600 dark:text-amber-400")}
              data-testid="workflow-run-state"
            >
              {state.label}
            </span>
            {run.archived && <span>Archived</span>}
          </span>
        </span>
      </Link>
    </li>
  );
}

function RunStateIcon({ state }: { state: WorkflowRunState }) {
  switch (state.kind) {
    case "running":
      return <Loader2Icon className="size-3.5 animate-spin text-muted-foreground" />;
    case "waiting":
    case "failed":
      return <TriangleAlertIcon className="size-4 text-amber-500" strokeWidth={1.75} />;
    case "done":
      return <CheckIcon className="size-4 text-muted-foreground" />;
    case "stopped":
      return <span aria-hidden className="size-2 rounded-full bg-muted-foreground/40" />;
  }
}
