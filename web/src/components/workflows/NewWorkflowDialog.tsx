import { type FormEvent, useEffect, useId, useState } from "react";
import { WorkflowIcon } from "lucide-react";

import { Label } from "@/components/scheduled/Label";
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
import { Textarea } from "@/components/ui/textarea";

// The server's limit on a workflow's name.
const NAME_MAX_CHARS = 80;
const NO_NAMES: readonly string[] = [];

/** What the user asked for in the "New workflow" dialog. */
export interface NewWorkflowRequest {
  /** The workflow's name, trimmed, e.g. `"Fix flaky tests"`. */
  name: string;
  /** What the workflow should do, trimmed; the agent drafts its steps from this. */
  description: string;
}

export interface NewWorkflowDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Moves on to choosing where the drafting session runs; the dialog closes after. */
  onCreate: (request: NewWorkflowRequest) => void;
  /** Names of the user's workflows; reusing one blocks Create. */
  takenNames?: readonly string[];
}

/** Ask for a new workflow's name and what it should do. */
export function NewWorkflowDialog({
  open,
  onOpenChange,
  onCreate,
  takenNames = NO_NAMES,
}: NewWorkflowDialogProps) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const nameId = useId();
  const nameTakenId = useId();
  const descriptionId = useId();

  // Each opening starts empty.
  useEffect(() => {
    if (!open) return;
    setName("");
    setDescription("");
  }, [open]);

  const trimmedName = name.trim();
  const nameTaken = trimmedName !== "" && takenNames.includes(trimmedName);
  const canCreate = trimmedName !== "" && description.trim() !== "" && !nameTaken;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!canCreate) return;
    onCreate({ name: trimmedName, description: description.trim() });
    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="sm:max-w-lg max-h-[85dvh] grid-rows-[auto_1fr_auto]"
        data-testid="new-workflow-dialog"
      >
        <form className="contents" onSubmit={submit}>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <WorkflowIcon className="size-4" />
              <span>New workflow</span>
            </DialogTitle>
            <DialogDescription>
              The agent drafts the steps in a new session. You review them in chat before the
              workflow is saved.
            </DialogDescription>
          </DialogHeader>

          {/* The room keeps the scroll area from clipping the fields' focus rings. */}
          <div className="-mx-1 min-h-0 space-y-4 overflow-y-auto px-1">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={nameId}>Name</Label>
              <Input
                id={nameId}
                value={name}
                onChange={(event) => setName(event.currentTarget.value)}
                maxLength={NAME_MAX_CHARS}
                placeholder="Name this workflow"
                autoFocus
                aria-invalid={nameTaken}
                aria-describedby={nameTaken ? nameTakenId : undefined}
                className="text-ui focus-visible:ring-0"
                data-testid="new-workflow-name"
                componentId="workflows.new.name"
              />
              {nameTaken && (
                <p id={nameTakenId} role="status" className="text-sm text-warning">
                  You already have a workflow with this name.
                </p>
              )}
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={descriptionId}>What should it do?</Label>
              <Textarea
                id={descriptionId}
                value={description}
                onChange={(event) => setDescription(event.currentTarget.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) submit(event);
                }}
                maxLength={4000}
                rows={5}
                className="min-h-32 resize-y rounded-xl p-4 text-ui focus-visible:ring-0"
                placeholder="Run the test suite a few times, file an issue for each flaky test, and fix the clear ones"
                data-testid="new-workflow-description"
                componentId="workflows.new.description"
              />
            </div>
          </div>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              componentId="workflows.new.cancel"
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={!canCreate}
              data-testid="new-workflow-next"
              componentId="workflows.new.next"
            >
              Next
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
