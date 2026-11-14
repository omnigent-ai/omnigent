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
import { continuePlanMessage, taskActionMessage } from "@/lib/workflow";
import { PlanRunButton, type PlanRunControl } from "@/shell/PlanRunButton";
import { ProgressStrip, taskMenuAction, type TaskMenuAction } from "@/shell/TasksPanel";
import { openTasksTab } from "@/store/sessionTasksActions";

/** The queries that list a run, e.g. `["workflow-runs", workflowId]`. */
type RunRefreshKeys = readonly (readonly unknown[])[];

/**
 * Messages or stops a run's session from a page other than its chat, then
 * refreshes the lists that show the run.
 */
function useRunSession(sessionId: string, refreshKeys: RunRefreshKeys) {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState(false);
  const act = async (request: () => Promise<void>) => {
    setPending(true);
    try {
      await request();
      for (const queryKey of refreshKeys) void queryClient.invalidateQueries({ queryKey });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Couldn't reach the session.");
    } finally {
      setPending(false);
    }
  };
  return {
    pending,
    send: (text: string) =>
      void act(async () => {
        const result = await sendUserMessage(sessionId, text);
        if (result.denied) toast.error("A policy blocked the message.");
      }),
    stop: () =>
      void act(async () => {
        await interrupt(sessionId);
      }),
  };
}

/**
 * A run's task menus, for its timeline's cards and its graph's nodes: each
 * runs, continues, retries or stops its task in the run's session.
 *
 * @param sessionId - The run's session; null when it has none, or takes no actions.
 * @returns What a task's ⋯ menu offers, or undefined without a session.
 */
export function useRunTaskMenus(
  sessionId: string | null | undefined,
  refreshKeys: RunRefreshKeys,
): ((item: StepProgress) => TaskMenuAction | undefined) | undefined {
  const session = useRunSession(sessionId ?? "", refreshKeys);
  if (!sessionId) return undefined;
  return (item) =>
    taskMenuAction(
      item,
      (action, taskName) => session.send(taskActionMessage(action, taskName)),
      session.stop,
    );
}

/**
 * A run's tasks as the Tasks tab's timeline, above the row's link. A segment
 * opens that task in the run's session.
 */
export function RunTimeline({
  sessionId,
  progress,
  menuActionFor,
  className,
}: {
  sessionId: string;
  progress: readonly StepProgress[];
  /** What a task's card offers in its ⋯ menu, from {@link useRunTaskMenus}; none when absent. */
  menuActionFor?: (item: StepProgress) => TaskMenuAction | undefined;
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
        menuActionFor={menuActionFor}
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
  refreshKeys: RunRefreshKeys;
  className?: string;
}) {
  const session = useRunSession(sessionId, refreshKeys);
  return (
    <PlanRunButton
      control={control}
      disabled={session.pending}
      className={cn("z-10", className)}
      onClick={() =>
        control === "stop" ? session.stop() : session.send(continuePlanMessage(planName))
      }
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
