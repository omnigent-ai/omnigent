// A pull request's lifecycle as a tinted pill: Open, Draft, Merged or Closed. The PR headers
// show it beside the repository link; the narrowest page header keeps just its icon.

import {
  GitMergeIcon,
  GitPullRequestClosedIcon,
  GitPullRequestDraftIcon,
  GitPullRequestIcon,
  type LucideIcon,
} from "lucide-react";

import { Shimmer } from "@/components/ai-elements/shimmer";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

interface LifecycleVisual {
  label: string;
  className: string;
  /** The state's color alone, for text that names the PR inline. */
  textClassName: string;
  Icon: LucideIcon;
}

/** The pill's label, tint and icon for a PR's state. */
export function pullRequestLifecycleVisual(state: string, isDraft = false): LifecycleVisual {
  const normalized = state.toUpperCase();
  if (normalized === "OPEN" && isDraft) {
    return {
      label: "Draft",
      className: "border-border bg-muted text-muted-foreground",
      textClassName: "text-muted-foreground",
      Icon: GitPullRequestDraftIcon,
    };
  }
  if (normalized === "OPEN") {
    return {
      label: "Open",
      className: "border-status-green/25 bg-status-green/10 text-status-green",
      textClassName: "text-status-green",
      Icon: GitPullRequestIcon,
    };
  }
  if (normalized === "MERGED") {
    return {
      label: "Merged",
      className: "border-status-purple/25 bg-status-purple/10 text-status-purple",
      textClassName: "text-status-purple",
      Icon: GitMergeIcon,
    };
  }
  if (normalized === "CLOSED") {
    return {
      label: "Closed",
      className: "border-status-red/25 bg-status-red/10 text-status-red",
      textClassName: "text-status-red",
      Icon: GitPullRequestClosedIcon,
    };
  }
  return {
    label: state,
    className: "border-border bg-muted text-muted-foreground",
    textClassName: "text-muted-foreground",
    Icon: GitPullRequestIcon,
  };
}

export function PullRequestLifecycleBadge({
  state,
  isDraft = false,
  compact = false,
  measuring = false,
  pending = false,
}: {
  state: string;
  isDraft?: boolean;
  /** Just the icon, its label kept as its name. */
  compact?: boolean;
  /** A hidden copy a header measures, which names nothing. */
  measuring?: boolean;
  /** A change to its state is on its way: its label shimmers, at the same width. */
  pending?: boolean;
}) {
  const visual = pullRequestLifecycleVisual(state, isDraft);
  const label = pending ? <Shimmer as="span">{visual.label}</Shimmer> : visual.label;
  return (
    <Badge
      aria-label={measuring ? undefined : `Pull request status: ${visual.label}`}
      // Its icon alone says less than its label, which the tooltip keeps.
      title={compact && !measuring ? visual.label : undefined}
      className={cn(
        "h-5 rounded-full border py-px text-xs leading-none",
        compact ? "px-1.5" : "px-2",
        visual.className,
      )}
    >
      {compact ? (
        <visual.Icon aria-hidden className={cn("size-3", pending && "motion-safe:animate-pulse")} />
      ) : (
        label
      )}
    </Badge>
  );
}
