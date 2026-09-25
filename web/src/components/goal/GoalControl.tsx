import { useEffect, useState } from "react";
import { GoalIcon, TargetIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { Goal } from "@/lib/goalApi";
import { cn } from "@/lib/utils";
import { CommandGoalDialog } from "./CommandGoalDialog";
import { GoalDialog } from "./GoalDialog";
import { formatGoalStatus } from "./goalUtils";

interface GoalControlBaseProps {
  conversationId: string | null;
  readOnly: boolean;
  /** Optional backend name used in the control's accessible label and tooltip. */
  backendLabel?: string;
}

interface ManagedGoalControlProps extends GoalControlBaseProps {
  mode?: "managed";
  goal: Goal | null;
  onGoalChange: (goal: Goal | null) => void;
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
          onGoalChange={props.onGoalChange}
        />
      )}
    </>
  );
}

/** Icon-only workspace-bar indicator for the current goal; details on hover, dialog on click. */
export function GoalStatusPill({ goal, onOpen }: { goal: Goal; onOpen?: () => void }) {
  const done = goal.status === "complete";
  const Icon = done ? GoalIcon : TargetIcon;
  const label = `Goal ${formatGoalStatus(goal.status)}`;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          data-testid="composer-goal-mode"
          data-state={done ? "done" : "working"}
          onClick={onOpen}
          aria-label={`${label}: ${goal.objective}`}
          className="flex shrink-0 items-center rounded-full bg-transparent px-1 text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 md:px-2"
        >
          <Icon className="size-3.5" strokeWidth={1.5} aria-hidden="true" />
        </button>
      </TooltipTrigger>
      <TooltipContent side="top" align="end" className="flex-col items-start gap-0.5">
        <div className="font-medium">{label}</div>
        <div className="line-clamp-3">{goal.objective}</div>
      </TooltipContent>
    </Tooltip>
  );
}
