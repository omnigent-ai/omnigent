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

import type { ShellBusy } from "./shellCloseCheck";

interface CloseShellDialogProps {
  /** Open when a tab close is awaiting confirmation. */
  open: boolean;
  /** Display label of the shell being closed (name · session), for the copy. */
  shellLabel: string | null;
  /** How many shells the confirmation closes; more than one uses plural copy. */
  shellCount?: number;
  /** What the shell runs, when its runner said; the copy names it. */
  busy?: ShellBusy | null;
  /** Confirm — kill the terminal. */
  onConfirm: () => void;
  /** Dismiss without closing the terminal. */
  onCancel: () => void;
}

/** "2 background jobs", for the copy. */
function backgroundJobsText(count: number): string {
  return count === 1 ? "1 background job" : `${count} background jobs`;
}

/** What runs in the shell, e.g. "vim and 1 background job", or null when nothing was named. */
export function busyShellText(busy: ShellBusy | null | undefined): string | null {
  if (!busy) return null;
  const jobs = busy.backgroundJobs ? backgroundJobsText(busy.backgroundJobs) : null;
  if (busy.foreground) return jobs ? `${busy.foreground} and ${jobs}` : busy.foreground;
  return jobs;
}

export function CloseShellDialog({
  open,
  shellLabel,
  shellCount = 1,
  busy,
  onConfirm,
  onCancel,
}: CloseShellDialogProps) {
  const several = shellCount > 1;
  const running = busyShellText(busy);
  const oneThing = busy?.foreground ? !busy.backgroundJobs : busy?.backgroundJobs === 1;
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onCancel()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{several ? `Close ${shellCount} shells?` : "Close shell?"}</DialogTitle>
          <DialogDescription>
            {several ? (
              `This terminates ${shellCount} shells and ends any processes running in them. This can't be undone.`
            ) : running && shellLabel ? (
              <>
                <span className="text-foreground">{running}</span> {oneThing ? "is" : "are"} running
                in <span className="text-foreground">{shellLabel}</span>. Closing the shell ends
                everything running in it. This can't be undone.
              </>
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
