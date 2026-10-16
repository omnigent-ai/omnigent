// The Pull Requests panel's list of every PR the session tracks. Open PRs come
// first; merged and closed PRs sit behind a fold. Each row reads its PR through
// the panel's own query key, so selecting a row reuses the cached PR page.

import { useEffect, useId, useState } from "react";
import { useQueries } from "@tanstack/react-query";
import {
  ChevronRightIcon,
  CircleCheckIcon,
  CircleDotIcon,
  CircleXIcon,
  GitMergeIcon,
  GitPullRequestClosedIcon,
  GitPullRequestDraftIcon,
  GitPullRequestIcon,
  Loader2Icon,
  type LucideIcon,
} from "lucide-react";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { gitProviderCopy } from "@/lib/gitProviders";
import { cn } from "@/lib/utils";
import {
  fetchPullRequestInfo,
  type PullRequest,
  type PullRequestAssociation,
} from "@/hooks/usePullRequests";
import {
  runnerOfflineRetryDelay,
  shouldRetryRunnerOffline,
} from "@/hooks/useWorkspaceChangedFiles";

/** The first load waits this long for every PR before showing what has resolved. */
export const FIRST_LOAD_REVEAL_MS = 5_000;

type Bucket = "open" | "merged" | "closed" | "loading";

interface PullRequestRow {
  association: PullRequestAssociation;
  pr: PullRequest | null;
  bucket: Bucket;
}

function bucketOf(pr: PullRequest | null, pending: boolean): Bucket {
  if (!pr) return pending ? "loading" : "open";
  const state = pr.state.toUpperCase();
  if (state === "MERGED") return "merged";
  if (state === "CLOSED") return "closed";
  return "open";
}

/**
 * Each tracked PR's state and checks, one panel-keyed query per PR.
 *
 * The first load is revealed as one batch once every lookup has settled
 * (a failure counts) or {@link FIRST_LOAD_REVEAL_MS} passes, so a slow or
 * broken forge cannot hold the others back; later updates land row by row.
 */
function usePullRequestRows(
  conversationId: string,
  associations: PullRequestAssociation[],
): { rows: PullRequestRow[]; revealed: boolean } {
  const results = useQueries({
    queries: associations.map((association) => ({
      queryKey: ["github-info", conversationId, association.url],
      queryFn: () => fetchPullRequestInfo(conversationId, association.url),
      retry: shouldRetryRunnerOffline,
      retryDelay: runnerOfflineRetryDelay,
      staleTime: 30_000,
      // Running checks refresh here; settled PRs refresh on turn end like the panel.
      refetchInterval: (query: { state: { data?: { pr?: PullRequest | null } } }) => {
        const pr = query.state.data?.pr;
        return pr?.state === "OPEN" && pr.checks.pending > 0 ? 15_000 : false;
      },
      refetchIntervalInBackground: false,
    })),
  });
  const settled = results.every((result) => !result.isPending);
  const [revealed, setRevealed] = useState(false);
  useEffect(() => {
    if (settled) setRevealed(true);
  }, [settled]);
  useEffect(() => {
    const timer = window.setTimeout(() => setRevealed(true), FIRST_LOAD_REVEAL_MS);
    return () => window.clearTimeout(timer);
  }, []);
  const rows = associations.map((association, index) => {
    const pr = results[index]?.data?.pr ?? null;
    return { association, pr, bucket: bucketOf(pr, results[index]?.isPending ?? true) };
  });
  return { rows, revealed: revealed || settled };
}

function stateVisual(row: PullRequestRow): { Icon: LucideIcon; label: string; className: string } {
  switch (row.bucket) {
    case "loading":
      return {
        Icon: Loader2Icon,
        label: "Loading",
        className: "animate-spin text-muted-foreground",
      };
    case "merged":
      return {
        Icon: GitMergeIcon,
        label: "Merged",
        className: "text-purple-600 dark:text-purple-400",
      };
    case "closed":
      return {
        Icon: GitPullRequestClosedIcon,
        label: "Closed",
        className: "text-red-600 dark:text-red-400",
      };
    default:
      return row.pr?.is_draft
        ? { Icon: GitPullRequestDraftIcon, label: "Draft", className: "text-muted-foreground" }
        : {
            Icon: GitPullRequestIcon,
            label: "Open",
            className: "text-green-600 dark:text-green-400",
          };
  }
}

/** Non-empty check buckets, failures first since they need attention. */
function checkBuckets(pr: PullRequest | null) {
  if (!pr || pr.checks_supported === false || pr.checks.total === 0) return [];
  return [
    {
      count: pr.checks.failing,
      outcome: "failed",
      BucketIcon: CircleXIcon,
      tint: "text-red-600 dark:text-red-400",
    },
    {
      count: pr.checks.pending,
      outcome: "pending",
      BucketIcon: CircleDotIcon,
      tint: "text-amber-600 dark:text-amber-400",
    },
    {
      count: pr.checks.passing,
      outcome: "passed",
      BucketIcon: CircleCheckIcon,
      tint: "text-green-600 dark:text-green-400",
    },
  ].filter((bucket) => bucket.count > 0);
}

/** A PR's repository and number, e.g. ``"acme/app #12 (from branch)"``. */
export function pullRequestIdentity(
  pr: PullRequestAssociation,
  sessionProvider?: string | null,
): string {
  const copy = gitProviderCopy(pr.provider ?? sessionProvider, undefined, pr.provider_display);
  const host = pr.host === copy.defaultHost ? "" : `${pr.host}/`;
  const inferred = pr.relationship === "inferred" ? " (from branch)" : "";
  return `${host}${pr.repository} ${copy.prNumberPrefix}${pr.number}${inferred}`;
}

function Row({
  row,
  sessionProvider,
  selected,
  onSelect,
}: {
  row: PullRequestRow;
  sessionProvider?: string | null;
  selected: boolean;
  onSelect: () => void;
}) {
  const { association, pr } = row;
  const descriptionId = useId();
  const identity = pullRequestIdentity(association, sessionProvider);
  const title = (pr?.title ?? association.title)?.trim();
  const label = title ? `${identity} — ${title}` : identity;
  const { Icon, label: state, className } = stateVisual(row);
  const checks = checkBuckets(pr);
  const description = checks.length
    ? `${state}. Checks: ${checks.map(({ count, outcome }) => `${count} ${outcome}`).join(", ")}`
    : state;
  return (
    <li>
      <Tooltip>
        {/* The trigger sits on a wrapper so the row keeps its own aria-describedby. */}
        <TooltipTrigger asChild>
          <div>
            <button
              type="button"
              aria-label={label}
              aria-describedby={descriptionId}
              aria-current={selected || undefined}
              onClick={onSelect}
              className={cn(
                "flex h-8 w-full cursor-pointer items-center gap-2 px-3 text-left",
                selected ? "bg-muted" : "hover:bg-muted/60",
              )}
            >
              <Icon aria-hidden className={cn("size-3.5 shrink-0", className)} />
              <span className="min-w-0 flex-1 truncate text-xs">
                <span className="text-muted-foreground">{identity}</span>
                {title && <span className="font-medium"> · {title}</span>}
              </span>
              {checks.length > 0 && (
                <span aria-hidden className="flex shrink-0 items-center gap-2 text-xs tabular-nums">
                  {checks.map(({ count, outcome, BucketIcon, tint }) => (
                    <span key={outcome} className={cn("inline-flex items-center gap-0.5", tint)}>
                      <BucketIcon className="size-3" />
                      {count}
                    </span>
                  ))}
                </span>
              )}
            </button>
          </div>
        </TooltipTrigger>
        <TooltipContent side="bottom" align="start" className="wrap-anywhere">
          {label}
        </TooltipContent>
      </Tooltip>
      <span id={descriptionId} className="sr-only">
        {description}
      </span>
    </li>
  );
}

/** Merged and closed PRs behind a toggle that slides open like the turn fold. */
function FinishedSection({ count, children }: { count: number; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger className="group flex w-full cursor-pointer items-center gap-1 px-3 py-2 text-xs text-muted-foreground hover:text-foreground">
        <ChevronRightIcon
          aria-hidden
          className="size-3.5 transition-transform duration-200 group-data-[state=open]:rotate-90"
        />
        {open ? "Hide" : "Show"} {count} merged &amp; closed
      </CollapsibleTrigger>
      <CollapsibleContent className="turn-fold-content">{children}</CollapsibleContent>
    </Collapsible>
  );
}

/**
 * Every PR the session tracks, with its state and checks.
 *
 * @param selectedUrl - The PR shown in the panel below the list.
 * @param onSelect - Called with a row's PR URL when it is clicked.
 */
export function PullRequestList({
  conversationId,
  associations,
  sessionProvider,
  selectedUrl,
  onSelect,
}: {
  conversationId: string;
  associations: PullRequestAssociation[];
  /** The session's provider, for PRs from hosts that omit their own. */
  sessionProvider?: string | null;
  selectedUrl?: string;
  onSelect: (url: string) => void;
}) {
  const { rows, revealed } = usePullRequestRows(conversationId, associations);
  if (!revealed) {
    return (
      <div className="shrink-0 border-b border-border py-1">
        <p role="status" className="flex h-8 items-center gap-2 px-3 text-xs text-muted-foreground">
          <Loader2Icon aria-hidden className="size-3.5 shrink-0 animate-spin" />
          Loading{" "}
          {associations.length === 1 ? "1 pull request" : `${associations.length} pull requests`}…
        </p>
      </div>
    );
  }
  const open = rows.filter((row) => row.bucket === "open" || row.bucket === "loading");
  // Merged before closed; each group keeps the session's order.
  const finished = [
    ...rows.filter((row) => row.bucket === "merged"),
    ...rows.filter((row) => row.bucket === "closed"),
  ];
  const renderRows = (items: PullRequestRow[]) =>
    items.map((row) => (
      <Row
        key={row.association.url}
        row={row}
        sessionProvider={sessionProvider}
        selected={row.association.url === selectedUrl}
        onSelect={() => onSelect(row.association.url)}
      />
    ));
  return (
    <div className="max-h-[40%] shrink-0 overflow-y-auto border-b border-border py-1">
      {/* Rows sit edge to edge, so moving to a neighbour must not count as reaching the tooltip. */}
      <TooltipProvider delayDuration={300} disableHoverableContent>
        <ul aria-label="Session pull requests">
          {renderRows(open)}
          {finished.length > 0 && (
            <li>
              <FinishedSection count={finished.length}>
                <ul aria-label="Merged and closed pull requests">{renderRows(finished)}</ul>
              </FinishedSection>
            </li>
          )}
        </ul>
      </TooltipProvider>
    </div>
  );
}
