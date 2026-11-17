import { useState } from "react";
import { PullRequestStatusIcon } from "@/components/PullRequestStatusIcon";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
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

/** The states a stored PR can have, in the summary's order, with their words. */
const SUMMARY_STATES: readonly [PullRequestStatus, string][] = [
  ["open", "open"],
  ["draft", "draft"],
  ["merged", "merged"],
  ["closed", "closed"],
  ["unknown", "unknown"],
];

/** The popover's word for each state; a stored PR has no checks or merge state. */
const STATE_LABELS: Partial<Record<PullRequestStatus, string>> = {
  open: "Open",
  draft: "Draft",
  merged: "Merged",
  closed: "Closed without merging",
  unknown: "Status unknown",
};

/**
 * One line for a run's PRs: `PR #212 merged` for one, `PRs: 2 merged, 1 closed`
 * for several.
 */
export function runPullRequestsSummary(pullRequests: readonly ScheduledTaskRunPullRequest[]) {
  if (pullRequests.length === 1) {
    const pr = pullRequests[0]!;
    const status = getPullRequestStatus(pr);
    const number = `${gitProviderCopy(pr.provider, undefined, pr.provider_display).prNumberPrefix}${pr.number}`;
    return status === "unknown" ? `PR ${number}` : `PR ${number} ${status}`;
  }
  const counts = new Map<PullRequestStatus, number>();
  for (const pr of pullRequests) {
    const status = getPullRequestStatus(pr);
    counts.set(status, (counts.get(status) ?? 0) + 1);
  }
  const parts = SUMMARY_STATES.flatMap(([status, word]) =>
    counts.has(status) ? [`${counts.get(status)} ${word}`] : [],
  );
  return `PRs: ${parts.join(", ")}`;
}

/**
 * The PRs one automation run created or worked on, as an underlined line such as
 * `PRs: 2 merged, 1 closed`. It opens a list of the PRs, each with the state its
 * host last fetched and a link that opens it in a new tab.
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
  const [open, setOpen] = useState(false);
  if (pullRequests.length === 0) return null;
  const summary = runPullRequestsSummary(pullRequests);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          data-testid="run-pull-requests"
          aria-label={`${summary}. Show pull requests`}
          className={cn(
            "max-w-56 truncate rounded text-xs text-muted-foreground underline decoration-muted-foreground/50 underline-offset-2 transition-colors hover:text-foreground hover:decoration-foreground focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none",
            className,
          )}
        >
          {summary}
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-96 gap-1 p-1.5" aria-label="Pull requests">
        <ul className="flex flex-col" data-testid="run-pull-request-list">
          {pullRequests.map((pr) => (
            <RunPullRequestRow key={pr.url} pr={pr} nowMs={nowMs} />
          ))}
        </ul>
      </PopoverContent>
    </Popover>
  );
}

function RunPullRequestRow({ pr, nowMs }: { pr: ScheduledTaskRunPullRequest; nowMs: number }) {
  const status = getPullRequestStatus(pr);
  const copy = gitProviderCopy(pr.provider, undefined, pr.provider_display);
  const number = `${copy.prNumberPrefix}${pr.number}`;
  const stateLabel = STATE_LABELS[status] ?? pullRequestStatusPresentation(status).label;
  const asOf = stateAsOf(pr.state_checked_at, nowMs);
  const title = pr.title?.trim() ?? "";
  const origin = pr.relationship === "created" ? "Opened by this run" : "Worked on by this run";
  const details = [
    `${pr.repository} ${number}`,
    asOf ? `${stateLabel} as of ${asOf}` : stateLabel,
    title,
    origin,
  ]
    .filter(Boolean)
    .join(" · ");
  const href = parseHttpUrl(pr.url)?.href;
  const content = (
    <>
      <PullRequestStatusIcon status={status} className="mt-0.5" />
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="flex min-w-0 items-baseline gap-1.5">
          <span className="shrink-0 text-foreground/80 tabular-nums">{number}</span>
          <span className="min-w-0 truncate text-foreground">{title || pr.repository}</span>
        </span>
        <span className="text-xs text-muted-foreground">
          <span className={pullRequestStatusPresentation(status).className}>{stateLabel}</span>
          {asOf && ` · as of ${asOf}`}
          {` · ${pr.repository}`}
        </span>
      </span>
    </>
  );
  const rowClassName = "flex items-start gap-2 rounded-md px-2 py-1.5 text-sm";
  return (
    <li
      data-testid="run-pull-request"
      data-pr-status={status}
      data-pr-relationship={pr.relationship}
    >
      {href ? (
        <a
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          title={details}
          aria-label={`${details} (opens in a new tab)`}
          className={cn(
            rowClassName,
            "transition-colors hover:bg-muted focus-visible:bg-muted focus-visible:outline-none",
          )}
        >
          {content}
        </a>
      ) : (
        <span title={details} className={rowClassName}>
          {content}
        </span>
      )}
    </li>
  );
}
