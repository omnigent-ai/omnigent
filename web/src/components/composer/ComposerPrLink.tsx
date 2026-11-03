import { ExternalLinkIcon, GitPullRequestIcon, Loader2Icon } from "lucide-react";
import { PullRequestStatusIcon } from "@/components/PullRequestStatusIcon";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import type { ComposerPullRequest } from "@/hooks/useComposerGitStatus";
import { useIsCoarsePointer } from "@/hooks/useIsCoarsePointer";
import { gitProviderCopy } from "@/lib/gitProviders";
import { parseHttpUrl } from "@/lib/openLinkInApp";
import {
  getPullRequestStatus,
  pullRequestStatusPresentation,
  type PullRequestStatus,
} from "@/lib/pullRequestStatus";
import { cn } from "@/lib/utils";

export type ComposerPrState = "loading" | "ready" | "unknown";
const NO_PULL_REQUESTS: readonly ComposerPullRequest[] = [];

interface PreviewRow {
  pr: ComposerPullRequest;
  status: PullRequestStatus;
  identity: string;
  title: string;
  href: string | undefined;
}

function previewRows(prs: readonly ComposerPullRequest[]): PreviewRow[] {
  return prs.map((pr) => {
    const copy = gitProviderCopy(pr.provider, undefined, pr.provider_display);
    return {
      pr,
      status: getPullRequestStatus(pr),
      identity: `${pr.repository} ${copy.prNumberPrefix}${pr.number}`.trim(),
      title: pr.title?.trim() ?? "",
      href: parseHttpUrl(pr.url)?.href,
    };
  });
}

/** Plain-text preview for assistive technology; the visible rows hold links. */
function previewDescription(rows: readonly PreviewRow[]): string {
  const entries = rows.map(({ status, identity, title }) =>
    [identity, pullRequestStatusPresentation(status).label, title].filter(Boolean).join(", "),
  );
  return `Pull requests: ${entries.join(". ")}`;
}

function OpenPullRequestLink({ href, identity }: { href: string; identity: string }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      data-testid="composer-pr-preview-open"
      aria-label={`Open ${identity} in a new tab`}
      title="Open in new tab"
      className="shrink-0 rounded text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
    >
      <ExternalLinkIcon className="size-3.5" aria-hidden />
    </a>
  );
}

/**
 * PR chip for the composer workspace bar: the session's associated pull
 * request(s) as a link that opens the workspace rail's Pull Requests tab.
 * Shows ``#123`` for one PR or ``N PRs`` for several. Self-nulls when there is
 * no PR or no way to open the tab (e.g. the landing window). Hovering previews
 * each PR's status, with a link beside its title that opens it in a new tab.
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
  const rows = previewRows(prs);

  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger asChild>{trigger}</TooltipTrigger>
        {/* The label replaces Radix's hidden copy of the content, which would
            otherwise duplicate each row's link for assistive technology. */}
        <TooltipContent
          data-testid="composer-pr-preview"
          aria-label={previewDescription(rows)}
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
            {rows.map(({ pr, status: rowStatus, identity, title, href }) => {
              const rowPresentation = pullRequestStatusPresentation(rowStatus);
              const openLink = href ? (
                <OpenPullRequestLink href={href} identity={identity} />
              ) : null;
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
                      <span className="flex min-w-0 items-center gap-1">
                        <span className="truncate text-xs text-muted-foreground" title={identity}>
                          {identity}
                        </span>
                        {!title && openLink}
                      </span>
                      <span className={cn("shrink-0 text-xs", rowPresentation.className)}>
                        {rowPresentation.label}
                      </span>
                    </div>
                    {title && (
                      <div className="flex min-w-0 items-center gap-1">
                        <span className="truncate text-ui font-medium" title={title}>
                          {title}
                        </span>
                        {openLink}
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
