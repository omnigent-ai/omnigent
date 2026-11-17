import { useEffect, useState } from "react";
import { CircleXIcon, GoalIcon, PauseCircleIcon, TargetIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { Goal } from "@/lib/goalApi";
import { cn } from "@/lib/utils";
import { CommandGoalDialog } from "./CommandGoalDialog";
import { GoalDialog } from "./GoalDialog";
import { goalStatusLabel, goalStatusTone } from "./goalUtils";

interface GoalControlBaseProps {
  conversationId: string | null;
  readOnly: boolean;
  /** Optional backend name used in the control's accessible label and tooltip. */
  backendLabel?: string;
}

interface ManagedGoalControlProps extends GoalControlBaseProps {
  mode?: "managed";
  goal: Goal | null;
  goalRevision: number;
  onGoalChange: (goal: Goal | null) => void;
  refreshGoal: () => Promise<void>;
}

interface CommandGoalControlProps extends GoalControlBaseProps {
  mode: "command";
  onStartGoal: (condition: string) => void;
}

type GoalControlProps = ManagedGoalControlProps | CommandGoalControlProps;

/** Toolbar button plus dialog for a goal-capable session. */
export function GoalControl(props: GoalControlProps) {
  const { conversationId, readOnly, backendLabel } = props;
  const [open, setOpen] = useState(false);
  const goalName = backendLabel ? `${backendLabel} goal` : "goal";
  const commandMode = props.mode === "command";
  const goal = commandMode ? null : props.goal;

  useEffect(() => {
    if (!conversationId) setOpen(false);
  }, [conversationId]);

  return (
    <>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            size="sm"
            variant={goal ? "secondary" : "ghost"}
            className={cn(
              "h-9 w-9 gap-0 px-0 text-sm md:h-8 @lg/composer-actions:w-auto @lg/composer-actions:gap-1.5 @lg/composer-actions:px-2",
              goal && "border border-ring/30 text-foreground",
            )}
            disabled={!conversationId || (commandMode && readOnly)}
            aria-pressed={commandMode ? undefined : goal != null}
            aria-label={
              goal ? `View ${goalName}` : commandMode ? `Start ${goalName}` : `Set ${goalName}`
            }
            data-testid="goal-toggle"
            data-active={goal ? "true" : undefined}
            onClick={() => setOpen(true)}
          >
            <TargetIcon className="size-3.5" />
            <span className="hidden @lg/composer-actions:inline">Goal</span>
          </Button>
        </TooltipTrigger>
        <TooltipContent>
          {goal ? `View ${goalName}` : commandMode ? `Start ${goalName}` : `Set ${goalName}`}
        </TooltipContent>
      </Tooltip>
      {commandMode ? (
        <CommandGoalDialog
          open={open}
          onOpenChange={setOpen}
          readOnly={readOnly}
          onStartGoal={props.onStartGoal}
          backendLabel={backendLabel}
        />
      ) : (
        <GoalDialog
          open={open}
          onOpenChange={setOpen}
          conversationId={conversationId}
          readOnly={readOnly}
          goal={goal}
          goalRevision={props.goalRevision}
          onGoalChange={props.onGoalChange}
          refreshGoal={props.refreshGoal}
        />
      )}
    </>
  );
}

const PILL_ICONS = {
  working: TargetIcon,
  paused: PauseCircleIcon,
  done: GoalIcon,
  failed: CircleXIcon,
} as const;

/**
 * Workspace-bar indicator for the current goal; details on hover, dialog on click.
 *
 * Running and finished goals show only an icon. A goal that stopped short of
 * its condition also names its state, so a paused goal is visible at a glance.
 */
export function GoalStatusPill({ goal, onOpen }: { goal: Goal; onOpen?: () => void }) {
  const tone = goalStatusTone(goal.status);
  const Icon = PILL_ICONS[tone];
  const label = `Goal ${goalStatusLabel(goal.status).toLowerCase()}`;
  const needsAttention = tone === "paused" || tone === "failed";
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          data-testid="composer-goal-mode"
          data-state={tone}
          onClick={onOpen}
          aria-label={`${label}: ${goal.objective}`}
          className={cn(
            "flex shrink-0 items-center gap-1 rounded-full bg-transparent px-1 text-xs text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 md:px-2",
            tone === "paused" && "text-warning hover:text-warning",
            tone === "failed" && "text-destructive hover:text-destructive",
          )}
        >
          <Icon className="size-3.5" strokeWidth={1.5} aria-hidden="true" />
          {needsAttention && (
            <span aria-hidden="true" data-testid="composer-goal-mode-label">
              {goalStatusLabel(goal.status)}
            </span>
          )}
        </button>
      </TooltipTrigger>
      <TooltipContent side="top" align="end" className="flex-col items-start gap-0.5">
        <div className="font-medium">{`Goal ${goalStatusLabel(goal.status).toLowerCase()}`}</div>
        <div className="line-clamp-3 wrap-anywhere">{goal.objective}</div>
        {goal.statusDetail && (
          <div className="line-clamp-3 wrap-anywhere text-muted-foreground">
            {goal.statusDetail}
          </div>
        )}
      </TooltipContent>
    </Tooltip>
  );
}
