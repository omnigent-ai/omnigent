import { type FormEvent, useEffect, useId, useRef, useState } from "react";
import { WorkflowIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";

// A run's plan takes the workflow's name, so it shares the plan-name limit.
const WORKFLOW_NAME_MAX_CHARS = 80;

export interface SaveWorkflowDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Asks the agent to save the plan under *name*; the dialog closes after. */
  onSave: (name: string) => void;
  /** The name the dialog opens with, e.g. the plan's. */
  initialName?: string;
  /** Why the request can't be sent right now; it disables Save. */
  blockedReason?: string | null;
}

/** Pick the name a session's plan is saved under as a reusable workflow. */
export function SaveWorkflowDialog({
  open,
  onOpenChange,
  onSave,
  initialName = "",
  blockedReason = null,
}: SaveWorkflowDialogProps) {
  const [name, setName] = useState(initialName);
  const nameId = useId();
  const initialNameRef = useRef(initialName);
  initialNameRef.current = initialName;

  // Each opening starts from the caller's name.
  useEffect(() => {
    if (open) setName(initialNameRef.current.slice(0, WORKFLOW_NAME_MAX_CHARS));
  }, [open]);

  const canSave = name.trim() !== "" && blockedReason === null;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    // React bubbles a portal's submit to the composer's form, which would send the draft.
    event.stopPropagation();
    if (!canSave) return;
    onSave(name.trim());
    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md" data-testid="save-workflow-dialog">
        <form className="contents" onSubmit={submit}>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <WorkflowIcon className="size-4" />
              <span>Save as workflow</span>
            </DialogTitle>
            <DialogDescription>
              The agent saves this plan as a workflow you can run again from Run a workflow. It
              leaves out the details that change from run to run.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-1.5">
            <label className="text-sm font-medium text-muted-foreground" htmlFor={nameId}>
              Name
            </label>
            <Input
              id={nameId}
              value={name}
              onChange={(event) => setName(event.currentTarget.value)}
              maxLength={WORKFLOW_NAME_MAX_CHARS}
              placeholder="Triage flaky tests"
              autoFocus
              data-testid="save-workflow-dialog-name"
              componentId="save_workflow_dialog.name"
            />
          </div>
          {blockedReason && <p className="text-ui text-muted-foreground">{blockedReason}</p>}
          <DialogFooter>
            <Button
              type="submit"
              disabled={!canSave}
              data-testid="save-workflow-dialog-save"
              componentId="save_workflow_dialog.save"
            >
              Save workflow
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
