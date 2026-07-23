// Placeholder for the "Create with Omnigent" flow — a conversational path where
// the user describes the task in natural language and the agent proposes the
// name / prompt / schedule.
//
// TODO: keep this inert until the conversational create flow has backend support.
// This dialog only exists so the "New task" menu's first option is wired and
// discoverable now.

import { SparklesIcon } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

export function CreateWithOmnigentDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent data-testid="create-with-omnigent-dialog" className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <SparklesIcon className="size-4 text-primary" />
            Create with Omnigent
          </DialogTitle>
          <DialogDescription>
            Describe a recurring task in plain language and Omnigent will draft the schedule and
            prompt for you.
          </DialogDescription>
        </DialogHeader>
        <div className="rounded-md border border-dashed border-border bg-muted/30 px-4 py-8 text-center text-sm text-muted-foreground">
          Coming soon. For now, use{" "}
          <span className="font-medium text-foreground">Set up manually</span> to create a task.
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
