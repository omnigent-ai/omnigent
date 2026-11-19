import { TriangleAlertIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { thingyErrorMessage } from "./thingyNavigation";

/** A failure in a thingy view: what failed, the server's reason, and Retry when it can. */
export function ThingyErrorNotice({
  title,
  error,
  onRetry,
  className,
}: {
  title: string;
  error?: unknown;
  onRetry?: () => void;
  className?: string;
}) {
  return (
    <div
      role="alert"
      className={cn(
        "flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-ui",
        className,
      )}
    >
      <TriangleAlertIcon className="mt-0.5 size-4 shrink-0 text-destructive" />
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="font-medium">{title}</span>
        {error !== undefined && (
          <span className="break-words text-muted-foreground">{thingyErrorMessage(error)}</span>
        )}
      </div>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}
