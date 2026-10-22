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
  /** Confirm — kill the terminal. */
  onConfirm: () => void;
  /** Dismiss without closing the terminal. */
  onCancel: () => void;
}

export function CloseShellDialog({ open, shellLabel, onConfirm, onCancel }: CloseShellDialogProps) {
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onCancel()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Close shell?</DialogTitle>
          <DialogDescription>
            {shellLabel ? (
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
            Close shell
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
