import { type FormEvent, useEffect, useId, useRef, useState } from "react";
import { WorkflowIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";

/** What the user asked for in the "Run a workflow" dialog. */
export interface WorkflowRequest {
  /** The goal, trimmed, e.g. `"Add retries to the uploader"`. */
  goal: string;
  /** Whether the agent's plan starts with the user reviewing it. */
  reviewPlanFirst: boolean;
}

export interface WorkflowDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Sends the workflow; the dialog closes after. */
  onRunWorkflow: (request: WorkflowRequest) => void;
  /** The goal the dialog opens with, e.g. the new-session composer's draft. */
  initialGoal?: string;
  /** Why the workflow can't be sent right now; it disables Run. */
  blockedReason?: string | null;
}

/** Ask for a workflow's goal, and whether to review the plan before work starts. */
export function WorkflowDialog({
  open,
  onOpenChange,
  onRunWorkflow,
  initialGoal = "",
  blockedReason = null,
}: WorkflowDialogProps) {
  const [goal, setGoal] = useState(initialGoal);
  const [reviewPlanFirst, setReviewPlanFirst] = useState(true);
  const goalId = useId();
  const reviewId = useId();
  const initialGoalRef = useRef(initialGoal);
  initialGoalRef.current = initialGoal;

  // Each opening starts from the caller's goal, with the review checked.
  useEffect(() => {
    if (!open) return;
    setGoal(initialGoalRef.current);
    setReviewPlanFirst(true);
  }, [open]);

  const canRun = goal.trim() !== "" && blockedReason === null;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    // React bubbles a portal's submit to the composer's form, which would send the draft.
    event.stopPropagation();
    if (!canRun) return;
    onRunWorkflow({ goal: goal.trim(), reviewPlanFirst });
    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="sm:max-w-lg max-h-[85dvh] grid-rows-[auto_1fr_auto]"
        data-testid="workflow-dialog"
      >
        <form className="contents" onSubmit={submit}>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <WorkflowIcon className="size-4" />
              <span>Run a workflow</span>
            </DialogTitle>
            <DialogDescription>
              The agent turns your goal into a plan and works through it step by step. Follow along
              in the Tasks tab.
            </DialogDescription>
          </DialogHeader>

          <div className="min-h-0 space-y-4 overflow-y-auto">
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-muted-foreground" htmlFor={goalId}>
                Goal
              </label>
              <Textarea
                id={goalId}
                value={goal}
                onChange={(event) => setGoal(event.currentTarget.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) submit(event);
                }}
                maxLength={4000}
                className="min-h-28 resize-y"
                placeholder="Add retries to the uploader, with tests"
                autoFocus
                data-testid="workflow-dialog-goal"
                componentId="workflow_dialog.goal"
              />
            </div>
            <div className="space-y-1">
              <div className="flex items-center gap-2">
                <Checkbox
                  id={reviewId}
                  checked={reviewPlanFirst}
                  onCheckedChange={(checked) => setReviewPlanFirst(checked === true)}
                  data-testid="workflow-dialog-review-plan"
                  componentId="workflow_dialog.review_plan_first"
                />
                <label htmlFor={reviewId} className="cursor-pointer text-ui">
                  Review the plan before work starts
                </label>
              </div>
              <p className="pl-6 text-sm text-muted-foreground">
                The agent shows you its plan first and waits for your approval.
              </p>
            </div>
            {blockedReason && <p className="text-ui text-muted-foreground">{blockedReason}</p>}
          </div>

          <DialogFooter>
            <Button
              type="submit"
              disabled={!canRun}
              data-testid="workflow-dialog-run"
              componentId="workflow_dialog.run"
            >
              Run workflow
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
