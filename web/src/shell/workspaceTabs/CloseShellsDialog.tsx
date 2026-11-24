// Confirmation before a split's shells close together: closing the split's tab
// terminates every shell in it, so the person confirms once for all of them.

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

interface CloseShellsDialogProps {
  open: boolean;
  /** The shells' labels as one phrase, such as "zsh · s1 and bash · s1". */
  shellLabels: string | null;
  /** How many shells closing ends. */
  shellCount: number;
  onConfirm: () => void;
  onCancel: () => void;
}

export function CloseShellsDialog({
  open,
  shellLabels,
  shellCount,
  onConfirm,
  onCancel,
}: CloseShellsDialogProps) {
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onCancel()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{`Close ${shellCount} shells?`}</DialogTitle>
          <DialogDescription>
            This terminates <span className="text-foreground">{shellLabels}</span> and ends any
            process running in them. This can't be undone.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={onCancel}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={onConfirm}>
            Close shells
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
