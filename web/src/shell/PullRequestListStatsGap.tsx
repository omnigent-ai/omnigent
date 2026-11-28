import type { ReactNode } from "react";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  PULL_REQUEST_LIST_STATS_SORT_UNAVAILABLE,
  pullRequestListStatsCell,
  type PullRequestListStatsCell,
  type PullRequestListStatsField,
} from "@/lib/pullRequestListStats";
import { cn } from "@/lib/utils";

import type { PullRequestListEntry } from "./PullRequestListRows";

/** A row's Diff or Updated cell: its value, nothing yet, or why it has none. */
export function pullRequestListEntryStatsCell(
  entry: PullRequestListEntry,
  field: PullRequestListStatsField,
): PullRequestListStatsCell {
  return pullRequestListStatsCell(field, entry.row.stats, entry.statsGap, entry.statsService);
}

/** A dash for a value the list can't show, with the reason on hover and keyboard focus. */
export function PullRequestListStatsGapMark({
  label,
  reason,
  className,
}: {
  label: string;
  reason: string;
  className?: string;
}) {
  const description = `${label}. ${reason}`;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          role="img"
          tabIndex={0}
          aria-label={description}
          data-stats-gap
          className={cn(
            "cursor-default rounded-sm px-0.5 text-xs text-muted-foreground focus-visible:outline-2 focus-visible:outline-ring",
            className,
          )}
        >
          —
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">{description}</TooltipContent>
    </Tooltip>
  );
}

/** The reason unavailable sort buttons point to; render one per list, outside its headers. */
export function PullRequestListUnavailableSortReason({ id }: { id: string }) {
  return (
    <span id={id} hidden>
      {PULL_REQUEST_LIST_STATS_SORT_UNAVAILABLE}
    </span>
  );
}

/**
 * A column header's sort button while its order needs stats the list doesn't have:
 * it keeps its place and label, says why on hover and focus, and does nothing.
 */
export function PullRequestListUnavailableSort({
  className,
  label,
  reasonId,
  children,
}: {
  className?: string;
  /** The button's accessible name, when it differs from its text. */
  label?: string;
  /** The list's {@link PullRequestListUnavailableSortReason}. */
  reasonId: string;
  children: ReactNode;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-disabled="true"
          aria-label={label}
          aria-describedby={reasonId}
          data-sort-unavailable
          className={cn("cursor-default", className)}
        >
          {children}
        </button>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">
        {PULL_REQUEST_LIST_STATS_SORT_UNAVAILABLE}
      </TooltipContent>
    </Tooltip>
  );
}
