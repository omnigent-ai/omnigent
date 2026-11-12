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
  /** What the user wants done, trimmed, e.g. `"Add retries to the uploader"`. */
  description: string;
  /** Whether the agent's plan starts with the user reviewing it. */
  reviewPlanFirst: boolean;
}

export interface WorkflowDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Sends the workflow; the dialog closes after. */
  onRunWorkflow: (request: WorkflowRequest) => void;
  /** The description the dialog opens with, e.g. the new-session composer's draft. */
  initialDescription?: string;
  /** Why the workflow can't be sent right now; it disables Run. */
  blockedReason?: string | null;
}

/** Ask for a workflow's description, and whether to review the plan before work starts. */
export function WorkflowDialog({
  open,
  onOpenChange,
  onRunWorkflow,
  initialDescription = "",
  blockedReason = null,
}: WorkflowDialogProps) {
  const [description, setDescription] = useState(initialDescription);
  const [reviewPlanFirst, setReviewPlanFirst] = useState(true);
  const descriptionId = useId();
  const reviewId = useId();
  const initialDescriptionRef = useRef(initialDescription);
  initialDescriptionRef.current = initialDescription;

  // Each opening starts from the caller's description, with the review checked.
  useEffect(() => {
    if (!open) return;
    setDescription(initialDescriptionRef.current);
    setReviewPlanFirst(true);
  }, [open]);

  const canRun = description.trim() !== "" && blockedReason === null;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    // React bubbles a portal's submit to the composer's form, which would send the draft.
    event.stopPropagation();
    if (!canRun) return;
    onRunWorkflow({ description: description.trim(), reviewPlanFirst });
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
              The agent turns your description into a plan and works through it step by step. Follow
              along in the Tasks tab.
            </DialogDescription>
          </DialogHeader>

          {/* The room keeps the scroll area from clipping the fields' focus rings. */}
          <div className="-mx-1 min-h-0 space-y-4 overflow-y-auto px-1">
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-muted-foreground" htmlFor={descriptionId}>
                Description
              </label>
              <Textarea
                id={descriptionId}
                value={description}
                onChange={(event) => setDescription(event.currentTarget.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) submit(event);
                }}
                maxLength={4000}
                rows={5}
                // The New automation dialog's prompt field: focus recolors the border, with no ring.
                className="min-h-32 resize-y rounded-xl p-4 text-ui focus-visible:ring-0"
                placeholder="Add retries to the uploader, with tests"
                autoFocus
                data-testid="workflow-dialog-description"
                componentId="workflow_dialog.description"
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
                After exploring, the agent shows you its plan and waits for your approval.
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
