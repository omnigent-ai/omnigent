import { PullRequestStatusIcon } from "@/components/PullRequestStatusIcon";
import { stateAsOf } from "@/hooks/useSessionPullRequests";
import { gitProviderCopy } from "@/lib/gitProviders";
import { parseHttpUrl } from "@/lib/openLinkInApp";
import {
  getPullRequestStatus,
  pullRequestStatusPresentation,
  type PullRequestStatus,
} from "@/lib/pullRequestStatus";
import type { ScheduledTaskRunPullRequest } from "@/lib/scheduledTasksApi";
import { cn } from "@/lib/utils";

/** The chip's word for each state; a stored PR has no checks or merge state. */
const STATE_LABELS: Partial<Record<PullRequestStatus, string>> = {
  open: "Open",
  draft: "Draft",
  merged: "Merged",
  closed: "Closed",
  unknown: "Status unknown",
};

/** A closed PR is one closed without merging; merged ones say Merged. */
const STATE_DESCRIPTIONS: Partial<Record<PullRequestStatus, string>> = {
  closed: "Closed without merging",
};

function describe(pr: ScheduledTaskRunPullRequest, nowMs: number) {
  const status = getPullRequestStatus(pr);
  const copy = gitProviderCopy(pr.provider, undefined, pr.provider_display);
  const identity = `${pr.repository} ${copy.prNumberPrefix}${pr.number}`;
  const stateLabel = STATE_LABELS[status] ?? pullRequestStatusPresentation(status).label;
  const asOf = stateAsOf(pr.state_checked_at, nowMs);
  const stateText = [STATE_DESCRIPTIONS[status] ?? stateLabel, asOf && `as of ${asOf}`]
    .filter(Boolean)
    .join(" ");
  const origin = pr.relationship === "created" ? "Opened by this run" : "Worked on by this run";
  const title = pr.title?.trim() ?? "";
  return {
    status,
    stateLabel,
    number: `${copy.prNumberPrefix}${pr.number}`,
    title,
    href: parseHttpUrl(pr.url)?.href,
    label: [identity, stateText, title, origin].filter(Boolean).join(" · "),
  };
}

/**
 * The PRs one automation run created or worked on, each with the state its host
 * last fetched: open, draft, merged, or closed without merging. Each chip opens
 * the PR in a new tab.
 */
export function RunPullRequests({
  pullRequests,
  nowMs,
  className,
}: {
  pullRequests: readonly ScheduledTaskRunPullRequest[];
  nowMs: number;
  className?: string;
}) {
  if (pullRequests.length === 0) return null;
  return (
    <ul
      className={cn("flex flex-wrap gap-1.5", className)}
      aria-label="Pull requests"
      data-testid="run-pull-requests"
    >
      {pullRequests.map((pr) => {
        const view = describe(pr, nowMs);
        const presentation = pullRequestStatusPresentation(view.status);
        const content = (
          <>
            <PullRequestStatusIcon status={view.status} />
            <span className={cn("font-medium", presentation.className)}>{view.stateLabel}</span>
            <span className="text-foreground/80 tabular-nums">{view.number}</span>
            {view.title && <span className="min-w-0 truncate">{view.title}</span>}
          </>
        );
        const chipClassName =
          "inline-flex max-w-[22rem] items-center gap-1 rounded-md border border-border/70 px-1.5 py-0.5 text-xs text-muted-foreground";
        return (
          <li
            key={pr.url}
            className="min-w-0"
            data-testid="run-pull-request"
            data-pr-status={view.status}
            data-pr-relationship={pr.relationship}
          >
            {view.href ? (
              <a
                href={view.href}
                target="_blank"
                rel="noopener noreferrer"
                title={view.label}
                aria-label={`${view.label} (opens in a new tab)`}
                className={cn(
                  chipClassName,
                  "transition-colors hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none",
                )}
              >
                {content}
              </a>
            ) : (
              <span title={view.label} className={chipClassName}>
                {content}
              </span>
            )}
          </li>
        );
      })}
    </ul>
  );
}
