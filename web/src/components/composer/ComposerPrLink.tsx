import { GitPullRequestIcon, Loader2Icon } from "lucide-react";
import { cn } from "@/lib/utils";

export type ComposerPrState = "loading" | "ready" | "unknown";

/**
 * PR chip for the composer workspace bar: the session's associated pull
 * request(s) as a GitHub link that opens the workspace rail's GitHub tab.
 * Shows ``#123`` for one PR or ``N PRs`` for several. Self-nulls when there is
 * no PR or no way to open the tab (e.g. the landing window).
 *
 * @param prCount - Number of PRs associated with the session.
 * @param prNumber - The primary PR's number, shown when ``prCount === 1``.
 * @param onOpen - Opens the GitHub tab; ``null`` hides the link.
 */
export function ComposerPrLink({
  state,
  prCount,
  prNumber,
  onOpen,
  className,
}: {
  state: ComposerPrState;
  prCount: number;
  prNumber: number | null;
  onOpen: (() => void) | null;
  className?: string;
}) {
  if (state === "loading") {
    return (
      <span
        data-testid="composer-pr-loading"
        className={cn("flex shrink-0 items-center gap-1 text-sm text-muted-foreground", className)}
      >
        <Loader2Icon className="size-3.5 animate-spin" aria-hidden />
        <span>Checking PR…</span>
      </span>
    );
  }
  if (state === "unknown") {
    return (
      <span
        data-testid="composer-pr-unknown"
        className={cn("flex shrink-0 items-center gap-1 text-sm text-muted-foreground", className)}
      >
        <GitPullRequestIcon className="size-3.5 shrink-0" aria-hidden />
        <span>PR unavailable</span>
      </span>
    );
  }
  if (prCount <= 0 || !onOpen) return null;

  const label = prCount > 1 ? `${prCount} PRs` : prNumber == null ? "1 PR" : `#${prNumber}`;

  return (
    <button
      type="button"
      data-testid="composer-pr-link"
      onClick={() => onOpen()}
      aria-label={label}
      title={prCount > 1 ? "View these PRs in the GitHub tab" : "View this PR in the GitHub tab"}
      className={cn(
        "group flex min-w-0 items-center gap-1 rounded text-sm text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
        className,
      )}
    >
      <GitPullRequestIcon className="size-3.5 shrink-0" aria-hidden />
      {/* Short and informative, so it stays when the bar collapses; a PR
          number that would truncate still asks the bar to collapse the
          directory and branch text, which frees the room it needs. */}
      <span
        data-workspace-collapse-label=""
        className="truncate tabular-nums underline-offset-2 group-hover:underline group-focus-visible:underline"
        title={label}
      >
        {label}
      </span>
    </button>
  );
}
