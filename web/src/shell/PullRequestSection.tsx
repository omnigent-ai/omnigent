// Pieces the Summary tab's Reviewers and Labels sections share, so the two sit level
// side by side and their chips look and behave alike.

import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";

/** A section title and its optional picker trigger, in a fixed-height row. */
export function PullRequestSectionHeader({
  id,
  title,
  action,
}: {
  id: string;
  title: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex items-center gap-1">
      <h3 id={id} className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
        {title}
      </h3>
      {action}
    </div>
  );
}

/** Base classes of a reviewer or label chip; callers add the shape. */
export const PULL_REQUEST_CHIP_CLASS =
  "inline-flex max-w-full min-w-0 items-center gap-1.5 border border-border bg-muted/20 px-2 py-0.5 text-xs";

/** A chip's trailing icon button, e.g. remove. */
export function PullRequestChipAction({
  label,
  title,
  icon: Icon,
  onClick,
}: {
  label: string;
  title?: string;
  icon: LucideIcon;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={title}
      onClick={onClick}
      className="shrink-0 cursor-pointer rounded-full p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground"
    >
      <Icon aria-hidden className="size-3" />
    </button>
  );
}
