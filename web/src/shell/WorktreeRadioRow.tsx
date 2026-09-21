import type { HostWorktree } from "@/hooks/useHostWorktrees";
import { copyText } from "@/lib/clipboard";
import { relativeTime } from "@/lib/relativeTime";
import { cn } from "@/lib/utils";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { CopyIcon, EllipsisIcon, FolderOpenIcon } from "lucide-react";

export const WORKTREE_RADIO_SPACIOUS_ROW_CLASS =
  "min-h-9 rounded-lg px-3 py-1 text-base leading-[1.6]";

export const WORKTREE_RADIO_SPACIOUS_INPUT_CLASS =
  "appearance-none rounded-full border border-muted-foreground/60 bg-background checked:border-[5px] checked:border-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2";

export const WORKTREE_RADIO_SELECTOR_ROW_CLASS =
  "h-7 shrink-0 rounded-md px-2 py-0 text-base leading-5";

export const WORKTREE_RADIO_SELECTOR_INPUT_CLASS = "sr-only";

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
  variant = "default",
  onOpen,
}: {
  worktree: HostWorktree;
  checked: boolean;
  name: string;
  onSelect: () => void;
  testId: string;
  className?: string;
  variant?: "default" | "selector" | "spacious";
  onOpen?: () => void;
}) {
  const spacious = variant === "spacious";
  const selector = variant === "selector";
  const displayName = worktreeDisplayName(worktree.path);
  const updatedLabel = worktreeUpdatedLabel(worktree.updated_at);
  const branchLabel = worktree.branch ?? "Detached HEAD";
  const statusLabel = worktree.detached ? "Detached" : "Checked out";

  return (
    <div
      className={cn(
        "flex min-w-0 items-center rounded-md text-sm transition-colors hover:bg-muted focus-within:bg-muted",
        checked && "bg-muted",
        spacious && WORKTREE_RADIO_SPACIOUS_ROW_CLASS,
        selector && WORKTREE_RADIO_SELECTOR_ROW_CLASS,
        className,
      )}
      data-testid={testId}
    >
      <Tooltip>
        <TooltipTrigger asChild>
          <label className="flex min-w-0 flex-1 cursor-pointer items-center gap-2">
            <input
              type="radio"
              name={name}
              checked={checked}
              onChange={onSelect}
              className={cn(
                "size-4 shrink-0 accent-primary",
                spacious && WORKTREE_RADIO_SPACIOUS_INPUT_CLASS,
                selector && WORKTREE_RADIO_SELECTOR_INPUT_CLASS,
              )}
              aria-label={`Use worktree ${displayName}`}
            />
            <span
              className={cn(
                "min-w-0 flex-1 truncate font-medium text-foreground",
                spacious && "leading-4",
                selector && "leading-5",
              )}
            >
              {displayName}
            </span>
            <span
              className={cn(
                "shrink-0 text-xs text-muted-foreground",
                spacious && "text-base leading-[1.6]",
                selector && "text-base leading-5",
              )}
            >
              {updatedLabel}
            </span>
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
      {spacious && onOpen && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              className="-mr-1 ml-1 flex size-6 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              aria-label={`Worktree actions for ${displayName}`}
              data-testid={`${testId}-actions`}
            >
              <EllipsisIcon className="size-4" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-40">
            <DropdownMenuItem onSelect={onOpen} disabled={!onOpen}>
              <FolderOpenIcon />
              Open folder
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => void copyText(worktree.path)}>
              <CopyIcon />
              Copy path
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  );
}
