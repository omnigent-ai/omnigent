import type { HostWorktree } from "@/hooks/useHostWorktrees";
import { relativeTime } from "@/lib/relativeTime";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

export function worktreeDisplayName(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).at(-1) ?? path;
}

export function worktreeUpdatedLabel(updatedAt: number | null | undefined): string {
  if (updatedAt == null) return "Unknown";
  return relativeTime(updatedAt * 1000) || "Unknown";
}

export function WorktreeRadioRow({
  worktree,
  checked,
  name,
  onSelect,
  testId,
  className,
  spacious = false,
}: {
  worktree: HostWorktree;
  checked: boolean;
  name: string;
  onSelect: () => void;
  testId: string;
  className?: string;
  spacious?: boolean;
}) {
  const displayName = worktreeDisplayName(worktree.path);
  const updatedLabel = worktreeUpdatedLabel(worktree.updated_at);
  const branchLabel = worktree.branch ?? "Detached HEAD";
  const statusLabel = worktree.detached ? "Detached" : "Checked out";

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <label
          className={cn(
            "flex min-w-0 cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm transition-colors hover:bg-muted focus-within:bg-muted",
            checked && "bg-muted",
            spacious && "min-h-11 rounded-lg px-3 text-base",
            className,
          )}
          data-testid={testId}
        >
          <input
            type="radio"
            name={name}
            checked={checked}
            onChange={onSelect}
            className={cn(
              "size-4 shrink-0 accent-primary",
              spacious &&
                "appearance-none rounded-full border border-muted-foreground/60 bg-background checked:border-[5px] checked:border-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
            )}
            aria-label={`Use worktree ${displayName}`}
          />
          <span className="min-w-0 flex-1 truncate font-medium text-foreground">{displayName}</span>
          <span className="shrink-0 text-xs text-muted-foreground">{updatedLabel}</span>
        </label>
      </TooltipTrigger>
      <TooltipContent
        side="right"
        className="max-w-sm flex-col items-start border border-border bg-popover text-popover-foreground shadow-menu ring-1 ring-foreground/10"
        data-testid={`${testId}-tooltip`}
      >
        <span className="break-all">
          <span className="font-semibold">Path:</span> {worktree.path}
        </span>
        <span>
          <span className="font-semibold">Branch:</span> {branchLabel}
        </span>
        <span>
          <span className="font-semibold">Status:</span> {statusLabel}
        </span>
      </TooltipContent>
    </Tooltip>
  );
}
