// Confirmation modal for closing a shell soft tab. Closing a tab kills the
// underlying terminal (its PTY is terminated server-side), so the close is
// destructive and irreversible — the user confirms first. Only user-initiated
// closes route through here; a terminal that goes away on its own (the agent
// closed it, or the PTY died) just drops its tab silently.

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

interface CloseShellDialogProps {
  /** Open when a tab close is awaiting confirmation. */
  open: boolean;
  /** Display label of the shell being closed (name · session), for the copy. */
  shellLabel: string | null;
  /** How many shells the confirmation closes; more than one uses plural copy. */
  shellCount?: number;
  /** Confirm — kill the terminal. */
  onConfirm: () => void;
  /** Dismiss without closing the terminal. */
  onCancel: () => void;
}

export function CloseShellDialog({
  open,
  shellLabel,
  shellCount = 1,
  onConfirm,
  onCancel,
}: CloseShellDialogProps) {
  const several = shellCount > 1;
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onCancel()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{several ? `Close ${shellCount} shells?` : "Close shell?"}</DialogTitle>
          <DialogDescription>
            {several ? (
              `This terminates ${shellCount} shells and ends any processes running in them. This can't be undone.`
            ) : shellLabel ? (
              <>
                This terminates <span className="text-foreground">{shellLabel}</span> and ends any
                process running in it. This can't be undone.
              </>
            ) : (
              "This terminates the shell and ends any process running in it. This can't be undone."
            )}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={onCancel}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={onConfirm}>
            {several ? "Close shells" : "Close shell"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
