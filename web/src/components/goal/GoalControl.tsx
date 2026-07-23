import { useEffect, useState } from "react";
import { TargetIcon } from "lucide-react";
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
              "h-9 gap-1.5 px-2 text-xs md:h-8",
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
            <span>Goal</span>
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

/** Compact status-line indicator for the current goal. */
export function GoalStatusPill({ goal }: { goal: Goal }) {
  return (
    <span
      data-testid="composer-goal-mode"
      className="inline-flex items-center gap-1 text-xs font-medium text-foreground"
    >
      <TargetIcon className="size-3.5 shrink-0" />
      <span>Goal {formatGoalStatus(goal.status)}</span>
    </span>
  );
}
