// Viewed-file controls for the pull request Changes tab: the per-file "Viewed"
// checkbox in each file's header, and the progress and "Hide viewed" row in the toolbar.
// The Changes tab renders them only while viewed state is available.

import { Checkbox } from "@/components/ui/checkbox";
import { Progress } from "@/components/ui/progress";

/** The "Viewed" checkbox in a file's header. */
export function PullRequestViewedToggle({
  path,
  viewed,
  onToggle,
}: {
  path: string;
  viewed: boolean;
  onToggle: () => void;
}) {
  return (
    <label className="flex shrink-0 cursor-pointer items-center gap-1.5 px-1 text-xs text-muted-foreground">
      <Checkbox checked={viewed} onCheckedChange={onToggle} aria-label={`Viewed ${path}`} />
      Viewed
    </label>
  );
}

/** The toolbar's viewed count, a thin bar, and the "Hide viewed" toggle. */
export function PullRequestViewedProgress({
  viewedCount,
  totalCount,
  partial,
  mobile,
  hideViewed,
  onHideViewedChange,
}: {
  viewedCount: number;
  totalCount: number;
  /** The changed-file list is truncated, so the total is a lower bound. */
  partial: boolean;
  /** Mobile shows the count only. */
  mobile: boolean;
  hideViewed: boolean;
  onHideViewedChange: (hide: boolean) => void;
}) {
  return (
    <div className="flex items-center gap-2 whitespace-nowrap text-xs text-muted-foreground">
      <span>{`${viewedCount} / ${totalCount} files viewed${partial ? " (partial)" : ""}`}</span>
      {!mobile && (
        <Progress
          aria-label="Files viewed"
          value={totalCount > 0 ? (viewedCount / totalCount) * 100 : 0}
          className="w-16"
        />
      )}
      <label className="flex items-center gap-1.5 px-1">
        <Checkbox
          checked={hideViewed}
          onCheckedChange={(checked) => onHideViewedChange(checked === true)}
        />
        Hide viewed
      </label>
    </div>
  );
}
