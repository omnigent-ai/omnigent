// How a change in flight and a refused one look, everywhere a change renders before its
// answer: see README.md beside this file.

import { Loader2Icon, TriangleAlertIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/** Removing an item strikes it; any other change mutes it. */
export type PendingChangeKind = "removing" | "changing";

/** Classes for the element a change in flight affects. */
export function pendingChangeClassName(kind: PendingChangeKind): string {
  return cn("opacity-60", kind === "removing" && "line-through");
}

/** The small spinner beside an element whose change is in flight. */
export function PendingChangeIcon({ className }: { className?: string }) {
  return (
    <Loader2Icon
      aria-hidden
      className={cn("size-3.5 animate-spin text-muted-foreground", className)}
    />
  );
}

/**
 * A refused change, beside the element it restored: what failed and why, with Retry when a
 * fresh attempt is safe, and Dismiss.
 */
export function ChangeFailedNotice({
  message,
  onRetry,
  onDismiss,
  children,
  className,
}: {
  message: string;
  onRetry?: () => void;
  onDismiss?: () => void;
  /** Further actions, e.g. Open for a change whose controls live elsewhere. */
  children?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      role="alert"
      className={cn(
        "flex flex-wrap items-start gap-x-2 gap-y-1 text-ui text-destructive",
        className,
      )}
    >
      <TriangleAlertIcon aria-hidden className="mt-0.5 size-3.5 shrink-0" />
      <span className="min-w-0 flex-1">{message}</span>
      <span className="flex shrink-0 items-center gap-1">
        {children}
        {onRetry && (
          <Button variant="outline" size="xs" onClick={onRetry}>
            Retry
          </Button>
        )}
        {onDismiss && (
          <Button variant="ghost" size="xs" onClick={onDismiss}>
            Dismiss
          </Button>
        )}
      </span>
    </div>
  );
}
