// The header's merge status badge beside the lifecycle pill: an integration's merge readiness, or
// GitHub's own status, named and tinted as the composer's chip names it. A status about checks
// opens the same list of checks the Pull Requests table's CI cell does; any other opens the
// Overview's merge section, where the status and what's behind it are.

import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import {
  pullRequestHeaderStatus,
  type PullRequestStatusDisplay,
} from "@/lib/pullRequestStatusDisplay";
import { cn } from "@/lib/utils";
import { MERGE_READINESS_BADGE_TONES } from "@/shell/PullRequestMergeReadiness";
import { CheckRunsPopover } from "@/shell/PullRequestChecks";
import { checkResult } from "@/shell/pullRequestListModel";
import type { PullRequestChecks } from "@/newPullRequestPage/hooks/usePullRequests";

/** Marks the Overview's merge status and checks, which the badge opens. */
export const MERGE_SECTION_ATTRIBUTE = "data-pull-request-merge-section";

/** What the header shows: a status, and whether it's the last one while a new commit loads. */
export interface HeaderMergeStatus {
  status: PullRequestStatusDisplay;
  /** Kept from before while the PR's new head is checked, so the badge doesn't flicker. */
  checking: boolean;
}

/** The PR's last answer that wasn't a check in progress: what it showed, or null for nothing. */
export interface ResolvedHeaderMergeStatus {
  prUrl: string;
  status: PullRequestStatusDisplay | null;
}

const isChecking = (display: PullRequestStatusDisplay | null | undefined) =>
  display?.key === "merge:checking";

/**
 * The header's status from the PR's current one and its last resolved answer. While a new
 * head is checked, a status the same PR last showed stays, marked as checking; an answer that
 * showed nothing, or another PR's, never stands in.
 */
export function steadyHeaderMergeStatus(
  prUrl: string,
  display: PullRequestStatusDisplay | null | undefined,
  last: ResolvedHeaderMergeStatus | null,
): HeaderMergeStatus | null {
  if (isChecking(display)) {
    return last?.prUrl === prUrl && last.status ? { status: last.status, checking: true } : null;
  }
  const shown = display ? pullRequestHeaderStatus(display) : null;
  return shown ? { status: shown, checking: false } : null;
}

function sameAnswer(
  left: ResolvedHeaderMergeStatus | null,
  right: ResolvedHeaderMergeStatus | null,
): boolean {
  if (left === null || right === null) return left === right;
  if (left.prUrl !== right.prUrl) return false;
  if (left.status === null || right.status === null) return left.status === right.status;
  return (
    left.status.key === right.status.key &&
    left.status.label === right.status.label &&
    left.status.detail === right.status.detail
  );
}

/**
 * {@link steadyHeaderMergeStatus} over renders. It remembers each resolved answer, quiet or
 * not, and forgets it once another PR shows, so only a check of the same PR keeps it.
 */
export function useSteadyHeaderMergeStatus(
  prUrl: string,
  display: PullRequestStatusDisplay | null | undefined,
): HeaderMergeStatus | null {
  const [last, setLast] = useState<ResolvedHeaderMergeStatus | null>(null);
  const resolved: ResolvedHeaderMergeStatus | null = isChecking(display)
    ? last?.prUrl === prUrl
      ? last
      : null
    : { prUrl, status: display ? pullRequestHeaderStatus(display) : null };
  // Compared by what it says, so a re-render with the same answer is a no-op.
  if (!sameAnswer(last, resolved)) setLast(resolved);
  return steadyHeaderMergeStatus(prUrl, display, resolved);
}

export type MergeStatusBadgeLength = "full" | "short" | "icon";

/** What the badge shows at each length; its label stays its accessible name. */
export function MergeStatusBadgeFace({
  status,
  length,
}: {
  status: PullRequestStatusDisplay;
  length: MergeStatusBadgeLength;
}) {
  if (length === "icon") return <status.Icon aria-hidden className="size-3" />;
  return length === "full" ? status.label : status.short;
}

/** The badge's box, shared with the header's measuring copies so they size alike. */
export function mergeStatusBadgeClass(status: PullRequestStatusDisplay): string {
  return cn(
    "h-5 rounded-full border px-2 py-px text-xs leading-none",
    MERGE_READINESS_BADGE_TONES[status.tone],
  );
}

/** Statuses that are about the head commit's checks, so the badge lists them. */
const CHECKS_STATUSES = new Set([
  "failed",
  "pending",
  "merge:checks_failing",
  "merge:ci_failing",
  "merge:ci_running",
]);

export function PullRequestMergeStatusBadge({
  status,
  checking,
  length,
  onOpen,
  checks,
}: HeaderMergeStatus & {
  /** The full label where it fits, the short one in a narrow header, its icon in the narrowest. */
  length: MergeStatusBadgeLength;
  /** Show the Overview's merge section. */
  onOpen: () => void;
  /** The head commit's checks, listed by a checks status as the table's CI cell lists them. */
  checks?: PullRequestChecks;
}) {
  const listsChecks =
    !checking && CHECKS_STATUSES.has(status.key) && (checks?.runs?.length ?? 0) > 0;
  const reason = checking ? "Checking the latest commit…" : status.detail;
  // As an icon, the badge leaves its label to the tooltip, ahead of the reason.
  const tooltip = length === "icon" ? [status.label, reason].filter(Boolean).join(". ") : reason;
  const badge = (
    <Badge asChild className={cn(mergeStatusBadgeClass(status), "cursor-pointer")}>
      <button
        type="button"
        aria-label={`Merge status: ${status.label}. ${listsChecks ? "Show checks" : "Show details"}`}
        aria-description={reason}
        aria-busy={checking || undefined}
        data-merge-status={status.key}
        // Kept from before while the new commit is checked, so it reads as provisional.
        className={cn(
          "hover:brightness-95 focus-visible:outline-none dark:hover:brightness-125",
          checking && "opacity-60",
        )}
        onClick={listsChecks ? undefined : onOpen}
      >
        <MergeStatusBadgeFace status={status} length={length} />
      </button>
    </Badge>
  );
  const trigger = <TooltipTrigger asChild>{badge}</TooltipTrigger>;
  return (
    // Its own provider, so the header can show it wherever the page is mounted.
    <TooltipProvider>
      <Tooltip>
        {listsChecks && checks ? (
          // The same list of checks the Pull Requests table's CI cell opens.
          <CheckRunsPopover
            trigger={trigger}
            label={checkResult(checks).description}
            runs={checks.runs}
            titled={false}
          />
        ) : (
          trigger
        )}
        {tooltip && <TooltipContent>{tooltip}</TooltipContent>}
      </Tooltip>
    </TooltipProvider>
  );
}

/**
 * Bring the Overview's merge section into view and focus it, once the Overview has rendered
 * it: the badge switches tabs first. Gives up after a moment if the section never shows.
 */
export function revealMergeSection(root: HTMLElement | null): void {
  if (!root) return;
  const reveal = () => {
    const section = root.querySelector<HTMLElement>(`[${MERGE_SECTION_ATTRIBUTE}]`);
    if (!section || section.closest("[hidden]")) return false;
    section.scrollIntoView({ block: "start" });
    section.focus({ preventScroll: true });
    return true;
  };
  if (reveal()) return;
  const observer = new MutationObserver(() => {
    if (reveal()) stop();
  });
  const timer = window.setTimeout(() => stop(), 2000);
  function stop() {
    observer.disconnect();
    window.clearTimeout(timer);
  }
  observer.observe(root, { childList: true, subtree: true, attributes: true });
}
