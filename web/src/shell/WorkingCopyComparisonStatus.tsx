// What the Changes tab and the review say about a session's comparison when
// it isn't simply showing: still comparing, out of date, reconnecting, fallen
// back to the last commit, or unavailable. Each offers the way forward.

import { AlertCircleIcon, Loader2Icon, RefreshCwIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useChoosingComparisonOwner } from "@/hooks/useComparisonOwner";
import { workingCopyComparisonActions } from "@/hooks/useWorkingCopyChanges";
import type { EffectiveComparison } from "@/lib/workingCopyComparison";
import { cn } from "@/lib/utils";

/** A one-line note above the changes; renders nothing when there's nothing to say. */
export function WorkingCopyComparisonStatusRow({
  sessionId,
  comparison,
  className,
}: {
  sessionId: string;
  comparison: EffectiveComparison;
  className?: string;
}) {
  const actions = workingCopyComparisonActions(sessionId, useChoosingComparisonOwner());
  let message: string;
  let busy = false;
  let retry = false;
  let showLastCommit = false;
  let compareAgain = false;
  if (comparison.base === "branch-point" && comparison.status) {
    message = comparison.status.message;
    busy = comparison.status.kind === "reconnecting";
    retry = comparison.status.kind === "stale";
    showLastCommit = true;
  } else if (comparison.base === "head" && comparison.override) {
    message = "Showing uncommitted changes for this session.";
    compareAgain = true;
  } else if (comparison.base === "head" && comparison.retry && comparison.note) {
    message = comparison.note;
    retry = true;
  } else {
    return null;
  }
  return (
    <div
      role="status"
      data-testid="working-copy-comparison-status"
      className={cn(
        "flex shrink-0 flex-wrap items-center gap-x-2 gap-y-1 border-b border-border bg-muted/40 px-3 py-1.5 text-xs text-muted-foreground",
        className,
      )}
    >
      {busy ? (
        <Loader2Icon className="size-3.5 shrink-0 animate-spin" aria-hidden />
      ) : (
        <AlertCircleIcon className="size-3.5 shrink-0" aria-hidden />
      )}
      <span className="min-w-0 flex-1">{message}</span>
      {retry && (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-6 px-2"
          onClick={actions.retry}
        >
          Retry
        </Button>
      )}
      {showLastCommit && (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-6 px-2"
          onClick={actions.showLastCommit}
        >
          Show uncommitted changes
        </Button>
      )}
      {compareAgain && (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-6 px-2"
          onClick={actions.compareAgain}
        >
          Compare with branch point
        </Button>
      )}
    </div>
  );
}

/**
 * Fills the space the changes would take while there's no list to show: the
 * first comparison is coming, or the chosen branch point can't be made.
 * Renders nothing when there's a list.
 */
export function WorkingCopyComparisonPlaceholder({
  sessionId,
  comparison,
}: {
  sessionId: string;
  comparison: EffectiveComparison;
}) {
  const actions = workingCopyComparisonActions(sessionId, useChoosingComparisonOwner());
  if (comparison.base === "pending") {
    return (
      <div
        role="status"
        className="flex h-full items-center justify-center gap-2 p-8 text-ui text-muted-foreground"
      >
        <Loader2Icon className="size-4 animate-spin" aria-hidden />
        {comparison.message}
      </div>
    );
  }
  if (comparison.base !== "unavailable") return null;
  return (
    <div
      role="alert"
      data-testid="working-copy-comparison-unavailable"
      className="flex h-full flex-col items-center justify-center gap-3 p-8 text-center text-ui"
    >
      <p className="font-medium text-foreground">Branch point isn&apos;t available</p>
      <p className="max-w-md text-muted-foreground">{comparison.message}</p>
      <div className="flex flex-wrap items-center justify-center gap-2">
        {comparison.retry && (
          <Button type="button" variant="outline" size="sm" onClick={actions.retry}>
            <RefreshCwIcon />
            Retry
          </Button>
        )}
        <Button type="button" variant="outline" size="sm" onClick={actions.showLastCommit}>
          Show uncommitted changes
        </Button>
      </div>
    </div>
  );
}
