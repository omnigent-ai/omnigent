import { Loader2Icon, RotateCwIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { ContextLoadState } from "./contextLoadIndicator";

/**
 * A file header's note while its unchanged lines load, its Retry when they couldn't, or a
 * note when the full file didn't match the diff. The separators show the same state.
 */
export function ContextLoadStatus({
  path,
  state,
  onRetry,
}: {
  path: string;
  state: ContextLoadState | undefined;
  onRetry: () => void;
}) {
  // The separators say it in words; the header keeps the file's name and holds the control.
  if (state === "loading") {
    return (
      <span role="status" className="flex items-center text-muted-foreground">
        <Loader2Icon className="size-3.5 animate-spin motion-reduce:animate-none" aria-hidden />
        <span className="sr-only">Loading unchanged lines…</span>
      </span>
    );
  }
  if (state === "mismatched") {
    // Nothing to retry: the same text would load again.
    return (
      <span
        role="status"
        title="The full file doesn’t match this diff, so only the changed lines are shown."
        className="max-w-[min(18rem,30cqw)] truncate text-muted-foreground @max-[22rem]:sr-only"
      >
        Couldn’t load the full file
      </span>
    );
  }
  if (state !== "failed") return null;
  return (
    <span className="flex items-center">
      <span role="alert" className="sr-only">
        Couldn’t load unchanged lines
      </span>
      <Button
        type="button"
        variant="ghost"
        size="xs"
        aria-label={`Retry loading unchanged lines of ${path}`}
        title="Couldn’t load unchanged lines. Retry"
        className="text-destructive"
        onClick={(event) => {
          // The header toggles the file when clicked; Retry only loads.
          event.stopPropagation();
          onRetry();
        }}
      >
        <RotateCwIcon />
        <span className="@max-[22rem]:sr-only">Retry</span>
      </Button>
    </span>
  );
}
