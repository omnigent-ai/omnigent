// Pieces the Summary tab's Reviewers and Labels sections share, so the two sit level
// side by side and their chips look and behave alike.

import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";

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
  disabled = false,
}: {
  label: string;
  title?: string;
  icon: LucideIcon;
  onClick: () => void;
  /** Shown but inert, with `title` saying why; still reachable by keyboard. */
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={title}
      aria-disabled={disabled || undefined}
      onClick={disabled ? undefined : onClick}
      className={cn(
        "shrink-0 rounded-full p-0.5 text-muted-foreground",
        disabled
          ? "cursor-default opacity-50"
          : "cursor-pointer hover:bg-muted hover:text-foreground",
      )}
    >
      <Icon aria-hidden className="size-3" />
    </button>
  );
}
