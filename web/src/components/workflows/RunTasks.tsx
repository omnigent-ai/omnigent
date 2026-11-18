import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { WorkflowIcon } from "lucide-react";
import { toast } from "sonner";

import { FullscreenIcon } from "@/components/ai-elements/MermaidBlock";
import { Button } from "@/components/ui/button";
import { useSavedWorkflows } from "@/hooks/useSavedWorkflows";
import { workflowQuestionStep } from "@/lib/blocks";
import { messageDeepLinkParams } from "@/lib/messageDeepLink";
import type { StepProgress } from "@/lib/sessionJournal";
import { getSession, sendUserMessage } from "@/lib/sessionsApi";
import { Link, useNavigate } from "@/lib/routing";
import { parseEvent } from "@/lib/sse";
import { cn } from "@/lib/utils";
import { continuePlanMessage, stopPlanMessage, taskActionMessage } from "@/lib/workflow";
import { PlanRunButton, type PlanRunButtonState } from "@/shell/PlanRunButton";
import { ProgressStrip, taskMenuAction, type TaskMenuAction } from "@/shell/TasksPanel";
import { supersedePlanStop, trackPlanStop } from "@/store/planStops";
import { openTasksTab } from "@/store/sessionTasksActions";

/** The queries that list a run, e.g. `["workflow-runs", workflowId]`. */
type RunRefreshKeys = readonly (readonly unknown[])[];

// A run row's workflow link and graph button share one box, so they line up side by side.
const RUN_ICON_BUTTON_CLASS_NAME = "z-10 shrink-0 text-muted-foreground hover:text-foreground";

/**
 * Messages a run's session from a page other than its chat, then
 * refreshes the lists that show the run.
 */
function useRunSession(sessionId: string, refreshKeys: RunRefreshKeys, runId: string | null) {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState(false);
  const act = async (request: () => Promise<void>) => {
    setPending(true);
    try {
      await request();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Couldn't reach the session.");
    } finally {
      for (const queryKey of refreshKeys) void queryClient.invalidateQueries({ queryKey });
      setPending(false);
    }
  };
  // Resolves to the session's receipt, or null when it didn't take the message.
  const deliver = async (text: string) => {
    if (!runId) return null;
    const result = await sendUserMessage(sessionId, text, runId);
    if (!result.denied) return result;
    toast.error("A policy blocked the message.");
    return null;
  };
  // Any other message the session takes ends the Stop in progress when it was sent.
  const post = async (text: string) => {
    const endPlanStop = supersedePlanStop(sessionId);
    if (await deliver(text)) endPlanStop();
  };
  return {
    pending,
    act: (request: () => Promise<void>) => void act(request),
    send: (text: string) => void act(() => post(text)),
    stop: (planName: string | null) =>
      void act(() => trackPlanStop(sessionId, () => deliver(stopPlanMessage(planName)))),
    post,
  };
}

/** The question `sessionId`'s agent asked about a task and waits on, if one is pending. */
async function pendingQuestionFor(sessionId: string, stepId: string): Promise<string | null> {
  const session = await getSession(sessionId);
  for (const raw of session.pendingElicitations ?? []) {
    const event = parseEvent("response.elicitation_request", raw);
    if (
      event?.type === "elicitation_request" &&
      workflowQuestionStep(event, sessionId) === stepId
    ) {
      return event.elicitationId;
    }
  }
  return null;
}

/**
 * A run's task menus, for its timeline's cards and its graph's nodes: each
 * runs, continues, retries or stops its task in the run's session.
 *
 * @param sessionId - The run's session; null when it has none, or takes no actions.
 * @param stopping - Whether the run is already stopping, so its tasks offer no Stop.
 * @returns What a task's ⋯ menu offers, or undefined without a session.
 */
export function useRunTaskMenus(
  sessionId: string | null | undefined,
  refreshKeys: RunRefreshKeys,
  runId: string | null,
  stopping = false,
): ((item: StepProgress) => TaskMenuAction | undefined) | undefined {
  const session = useRunSession(sessionId ?? "", refreshKeys, runId);
  const navigate = useNavigate();
  if (!sessionId || !runId) return undefined;
  // The agent can't read a message while it waits on the task's question, so
  // Continue opens the session at the question instead.
  const proceed = (item: StepProgress) =>
    session.act(async () => {
      const questionId = await pendingQuestionFor(sessionId, item.step.id);
      if (questionId === null) {
        await session.post(taskActionMessage("proceed", item.step.name));
        return;
      }
      const search = messageDeepLinkParams(new URLSearchParams(), questionId);
      navigate(`/c/${encodeURIComponent(sessionId)}?${search}`);
    });
  return (item) => {
    const menuAction = taskMenuAction(
      item,
      (action, taskName) => session.send(taskActionMessage(action, taskName)),
      stopping ? undefined : () => session.stop(null),
    );
    return menuAction?.action === "proceed"
      ? { ...menuAction, onSelect: () => proceed(item) }
      : menuAction;
  };
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
 * Asks a run's main agent to stop or resume its work, including its workers.
 * The lists that show the run refresh once the session takes the message.
 */
export function RunPlanButton({
  sessionId,
  runId,
  planName,
  control,
  refreshKeys,
  className,
}: {
  sessionId: string;
  runId: string;
  /** The plan's name for the control message, e.g. the workflow's. */
  planName: string | null;
  control: PlanRunButtonState;
  refreshKeys: RunRefreshKeys;
  className?: string;
}) {
  const session = useRunSession(sessionId, refreshKeys, runId);
  return (
    <PlanRunButton
      control={control}
      disabled={session.pending}
      className={cn("z-10", className)}
      onClick={() =>
        control === "stop" ? session.stop(planName) : session.send(continuePlanMessage(planName))
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
      className={cn(RUN_ICON_BUTTON_CLASS_NAME, className)}
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
export function RunWorkflowButton({
  workflowId,
  className,
}: {
  workflowId: string;
  /** The row's placement, the same as its graph button's, e.g. `relative -my-1` on a title line. */
  className?: string;
}) {
  const { workflows } = useSavedWorkflows();
  const name = workflows.find((workflow) => workflow.id === workflowId)?.name;
  const label = name ? `Open workflow “${name}”` : "Open the workflow";
  return (
    <Button
      asChild
      variant="ghost"
      size="icon-sm"
      className={cn(RUN_ICON_BUTTON_CLASS_NAME, className)}
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
