import { GitPullRequestIcon, Loader2Icon } from "lucide-react";
import { PullRequestStatusIcon } from "@/components/PullRequestStatusIcon";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import type { ComposerPullRequest } from "@/hooks/useComposerGitStatus";
import { useIsCoarsePointer } from "@/hooks/useIsCoarsePointer";
import { gitProviderCopy } from "@/lib/gitProviders";
import {
  getPullRequestStatus,
  pullRequestStatusPresentation,
  type PullRequestStatus,
} from "@/lib/pullRequestStatus";
import { cn } from "@/lib/utils";

export type ComposerPrState = "loading" | "ready" | "unknown";
const NO_PULL_REQUESTS: readonly ComposerPullRequest[] = [];

/**
 * PR chip for the composer workspace bar: the session's associated pull
 * request(s) as a link that opens the workspace rail's Pull Requests tab.
 * Shows ``#123`` for one PR or ``N PRs`` for several. Self-nulls when there is
 * no PR or no way to open the tab (e.g. the landing window).
 *
 * @param prCount - Number of PRs associated with the session.
 * @param prNumber - The primary PR's number, shown when ``prCount === 1``.
 * @param prNumberPrefix - Precedes ``prNumber``; ``#`` for GitHub, ``!`` for Azure DevOps.
 * @param onOpen - Opens the Pull Requests tab; ``null`` hides the link.
 */
export function ComposerPrLink({
  state,
  prCount,
  prNumber,
  prNumberPrefix = "#",
  prs = NO_PULL_REQUESTS,
  prStatus = null,
  onOpen,
  className,
}: {
  state: ComposerPrState;
  prCount: number;
  prNumber: number | null;
  prNumberPrefix?: string;
  prs?: readonly ComposerPullRequest[];
  prStatus?: PullRequestStatus | null;
  onOpen: (() => void) | null;
  className?: string;
}) {
  const isCoarsePointer = useIsCoarsePointer();
  // Status belongs to a known PR; lookup and setup errors remain in the GitHub tab.
  if (prCount <= 0 || !onOpen) return null;

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
  const singleLabel = prNumber == null ? "1 PR" : `${prNumberPrefix}${prNumber}`;
  const label = prCount > 1 ? `${prCount} PRs` : singleLabel;
  const status = prStatus ?? "unknown";
  const presentation = pullRequestStatusPresentation(status);
  const showPreview = prs.length > 0;
  const showNativeHint = !showPreview || isCoarsePointer;
  const openHint =
    prCount > 1
      ? "View these PRs in the Pull Requests tab"
      : "View this PR in the Pull Requests tab";

  const trigger = (
    <button
      type="button"
      data-testid="composer-pr-link"
      data-pr-status={status}
      onClick={() => onOpen()}
      aria-label={label}
      aria-description={prStatus ? presentation.label : undefined}
      title={showNativeHint ? openHint : undefined}
      className={cn(
        "group flex min-w-0 items-center gap-1 rounded text-sm transition-colors hover:opacity-80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
        presentation.className,
        className,
      )}
    >
      <PullRequestStatusIcon status={status} colored={false} className="size-3.5 shrink-0" />
      {/* Short and informative, so it stays when the bar collapses; a PR
          number that would truncate still asks the bar to collapse the
          directory and branch text, which frees the room it needs. */}
      <span
        data-workspace-collapse-label=""
        className="truncate tabular-nums underline-offset-2 group-hover:underline group-focus-visible:underline"
        title={showNativeHint ? label : undefined}
      >
        {label}
      </span>
    </button>
  );

  if (!showPreview) return trigger;

  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger asChild>{trigger}</TooltipTrigger>
        <TooltipContent
          data-testid="composer-pr-preview"
          side="top"
          sideOffset={4}
          align="start"
          collisionPadding={16}
          className="block w-96 max-w-[calc(100vw-2rem)] p-0 shadow-menu ring-1 ring-foreground/10"
        >
          <div className="border-b border-border/50 px-3 py-2 text-xs font-medium text-muted-foreground">
            Pull requests
          </div>
          <ul aria-label="Pull requests by status" className="max-h-80 overflow-y-auto p-1">
            {prs.map((pr) => {
              const rowStatus = getPullRequestStatus(pr);
              const rowPresentation = pullRequestStatusPresentation(rowStatus);
              const copy = gitProviderCopy(pr.provider, undefined, pr.provider_display);
              const identity = `${pr.repository} ${copy.prNumberPrefix}${pr.number}`.trim();
              return (
                <li
                  key={pr.url}
                  data-pr-url={pr.url}
                  data-pr-status={rowStatus}
                  className="flex items-start gap-2 rounded-md px-2 py-2"
                >
                  <PullRequestStatusIcon status={rowStatus} className="mt-0.5 size-4 shrink-0" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="truncate text-xs text-muted-foreground" title={identity}>
                        {identity}
                      </span>
                      <span className={cn("shrink-0 text-xs", rowPresentation.className)}>
                        {rowPresentation.label}
                      </span>
                    </div>
                    {pr.title?.trim() && (
                      <div className="truncate text-ui font-medium" title={pr.title.trim()}>
                        {pr.title.trim()}
                      </div>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
