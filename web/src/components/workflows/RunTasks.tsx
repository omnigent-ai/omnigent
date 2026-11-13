import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { WorkflowIcon } from "lucide-react";
import { toast } from "sonner";

import { FullscreenIcon } from "@/components/ai-elements/MermaidBlock";
import { Button } from "@/components/ui/button";
import { useSavedWorkflows } from "@/hooks/useSavedWorkflows";
import type { StepProgress } from "@/lib/sessionJournal";
import { interrupt, sendUserMessage } from "@/lib/sessionsApi";
import { Link, useNavigate } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { continuePlanMessage } from "@/lib/workflow";
import { PlanRunButton, type PlanRunControl } from "@/shell/PlanRunButton";
import { ProgressStrip } from "@/shell/TasksPanel";
import { openTasksTab } from "@/store/sessionTasksActions";

/**
 * A run's tasks as the Tasks tab's timeline, above the row's link. A segment
 * opens that task in the run's session.
 */
export function RunTimeline({
  sessionId,
  progress,
  className,
}: {
  sessionId: string;
  progress: readonly StepProgress[];
  className?: string;
}) {
  const navigate = useNavigate();
  return (
    <div className={cn("relative z-10", className)}>
      <ProgressStrip
        progress={progress}
        fromPlan
        onSelect={(stepId) => {
          openTasksTab(sessionId, stepId);
          navigate(`/c/${encodeURIComponent(sessionId)}`);
        }}
      />
    </div>
  );
}

/**
 * A run's play or stop button, from a page other than its chat: stop
 * interrupts the run's session, and play asks it to continue the plan. The
 * lists that show the run refresh once the session takes it.
 */
export function RunPlanButton({
  sessionId,
  planName,
  control,
  refreshKeys,
  className,
}: {
  sessionId: string;
  /** The plan's name for the play message, e.g. the workflow's. */
  planName: string | null;
  control: PlanRunControl;
  /** The queries that list the run, e.g. `["workflow-runs", workflowId]`. */
  refreshKeys: readonly (readonly unknown[])[];
  className?: string;
}) {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState(false);
  const act = async () => {
    setPending(true);
    try {
      if (control === "stop") {
        await interrupt(sessionId);
      } else {
        const result = await sendUserMessage(sessionId, continuePlanMessage(planName));
        if (result.denied) toast.error("A policy blocked the message.");
      }
      for (const queryKey of refreshKeys) void queryClient.invalidateQueries({ queryKey });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Couldn't reach the session.");
    } finally {
      setPending(false);
    }
  };
  return (
    <PlanRunButton
      control={control}
      disabled={pending}
      className={cn("z-10", className)}
      onClick={() => void act()}
    />
  );
}

/** Opens a run on its workflow's graph, with the diagrams' "View fullscreen" icon. */
export function RunGraphButton({
  runName,
  onClick,
  className,
}: {
  /** Names the run for assistive technology, e.g. its session's title. */
  runName: string;
  onClick: () => void;
  className?: string;
}) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-sm"
      className={cn("z-10 shrink-0 text-muted-foreground hover:text-foreground", className)}
      aria-label={`View ${runName} on the graph`}
      title="View this run on the graph"
      data-testid="run-graph-button"
      onClick={onClick}
    >
      <FullscreenIcon />
    </Button>
  );
}

/** Opens the saved workflow a run follows, on the workflow's page. */
export function RunWorkflowButton({ workflowId }: { workflowId: string }) {
  const { workflows } = useSavedWorkflows();
  const name = workflows.find((workflow) => workflow.id === workflowId)?.name;
  const label = name ? `Open workflow “${name}”` : "Open the workflow";
  return (
    <Button
      asChild
      variant="ghost"
      size="icon-sm"
      className="z-10 shrink-0 text-muted-foreground hover:text-foreground"
    >
      <Link
        to={`/workflows/${encodeURIComponent(workflowId)}`}
        aria-label={label}
        title={label}
        data-testid="run-workflow-button"
      >
        <WorkflowIcon className="size-4" />
      </Link>
    </Button>
  );
}
