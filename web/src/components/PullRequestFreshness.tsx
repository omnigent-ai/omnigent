// The shared freshness mark: "Updated 12 min ago" while a surface shows pull request data kept
// on this device, and a quiet "refreshing" while GitHub is read again behind it. Static, so it
// reads the same under reduced motion; theme tokens only.

import { ClockIcon } from "lucide-react";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useNow } from "@/hooks/useNow";
import { freshnessLabel, type PullRequestFreshnessState } from "@/lib/pullRequestFreshness";
import { cn } from "@/lib/utils";

export function PullRequestFreshness({
  state,
  form = "words",
  describe,
  showAfter = 0,
  shortWhenNarrow = false,
  id,
  className,
}: {
  state: PullRequestFreshnessState | null;
  /**
   * `words` says "Updated 12 min ago"; `age` only "12 min ago", for a control that already says
   * what it updates; `clock` is a muted clock whose tooltip and accessible name say the age.
   */
  form?: "words" | "age" | "clock";
  /** What a `clock` says, from the age, such as "Reviewers as of 40 min ago". */
  describe?: (age: string) => string;
  /** Shows only once the data is at least this old, in ms. */
  showAfter?: number;
  /** Inside an `@container`: below its `lg` width, only the age and status show. */
  shortWhenNarrow?: boolean;
  /** For a control that names the mark as its description. */
  id?: string;
  className?: string;
}) {
  const now = useNow().getTime();
  const label = freshnessLabel(state, now);
  if (!state || !label || now - state.updatedAt < showAfter) return null;
  const mark = state.refreshing ? "refreshing" : state.failed ? "failed" : "rest";
  if (form === "clock") {
    const said = describe?.(label.age) ?? label.text;
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            role="img"
            aria-label={said}
            tabIndex={0}
            data-pull-request-freshness={mark}
            className={cn("inline-flex shrink-0 text-muted-foreground/80", className)}
          >
            <ClockIcon className="size-3" aria-hidden />
          </span>
        </TooltipTrigger>
        <TooltipContent>{said}</TooltipContent>
      </Tooltip>
    );
  }
  return (
    <span
      id={id}
      data-pull-request-freshness={mark}
      title={label.exact}
      className={cn("text-muted-foreground truncate text-xs whitespace-nowrap", className)}
    >
      {form === "age" ? (
        label.age
      ) : shortWhenNarrow ? (
        <>
          <span className="hidden @lg:inline">Updated </span>
          {label.age}
        </>
      ) : (
        label.text
      )}
      {label.status && ` · ${label.status}`}
    </span>
  );
}
