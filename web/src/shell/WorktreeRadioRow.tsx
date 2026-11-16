import type { ComponentProps } from "react";
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
import { MenuItem } from "@/components/ui/menu-item";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
  CircleCheckIcon,
  CircleDashedIcon,
  CopyIcon,
  EllipsisIcon,
  FolderIcon,
  FolderOpenIcon,
  GitBranchIcon,
} from "lucide-react";

export const WORKTREE_RADIO_SPACIOUS_ROW_CLASS = "h-7 rounded-md px-2 py-[3px] text-ui leading-4";

export const WORKTREE_RADIO_SELECTOR_ROW_CLASS =
  "h-7 shrink-0 rounded-md px-2 py-[3px] text-ui leading-4";

export const WORKTREE_RADIO_INDICATOR_CLASS =
  "pointer-events-none absolute inset-0 rounded-full border border-muted-foreground/60 bg-background peer-checked:border-[5px] peer-checked:border-primary peer-focus-visible:ring-2 peer-focus-visible:ring-ring peer-focus-visible:ring-offset-2";

/** Compact rows draw their own circle so engines' native radio layout cannot offset it. */
export function WorktreeRadio({
  compact,
  ...props
}: Omit<ComponentProps<"input">, "type" | "className"> & { compact: boolean }) {
  if (!compact) {
    return <input type="radio" className="size-4 shrink-0 accent-primary" {...props} />;
  }
  return (
    <span className="relative size-4 shrink-0">
      <input
        type="radio"
        className="peer absolute inset-0 m-0 size-full cursor-pointer appearance-none opacity-0"
        {...props}
      />
      <span aria-hidden className={WORKTREE_RADIO_INDICATOR_CLASS} />
    </span>
  );
}

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
  const StatusIcon = worktree.detached ? CircleDashedIcon : CircleCheckIcon;

  return (
    <MenuItem
      active={checked}
      density={spacious || selector ? "compact" : "none"}
      className={cn(
        variant === "default" && "text-sm",
        spacious && WORKTREE_RADIO_SPACIOUS_ROW_CLASS,
        selector && WORKTREE_RADIO_SELECTOR_ROW_CLASS,
        className,
      )}
      data-testid={testId}
    >
      <Tooltip>
        <TooltipTrigger asChild>
          <label className="flex min-w-0 flex-1 cursor-pointer items-center gap-2">
            <WorktreeRadio
              compact={spacious || selector}
              name={name}
              checked={checked}
              onChange={onSelect}
              aria-label={`Use worktree ${displayName}`}
            />
            <span
              className={cn(
                "min-w-0 flex-1 truncate font-medium text-foreground",
                spacious && "font-normal leading-4",
                selector && "font-normal leading-4",
              )}
            >
              {displayName}
            </span>
            <span
              className={cn(
                "shrink-0 text-xs text-muted-foreground",
                spacious && "leading-4",
                selector && "leading-4",
              )}
            >
              {updatedLabel}
            </span>
          </label>
        </TooltipTrigger>
        <TooltipContent
          side="right"
          align="start"
          alignOffset={spacious || selector ? -6 : 0}
          sideOffset={spacious ? 38 : selector ? 14 : 8}
          className="w-64 max-w-[calc(100vw-2rem)] flex-col items-stretch rounded-lg bg-popover p-2.5 text-popover-foreground whitespace-normal shadow-menu ring-1 ring-foreground/10"
          data-testid={`${testId}-tooltip`}
        >
          <p className="sidebar-compact-text font-medium" data-testid={`${testId}-tooltip-title`}>
            {displayName}
            <span className="font-normal text-muted-foreground">
              {" · "}
              {updatedLabel}
            </span>
          </p>
          <p
            className="mt-1 flex items-start gap-1.5 text-sm text-muted-foreground"
            data-testid={`${testId}-tooltip-path`}
          >
            <FolderIcon aria-hidden className="mt-0.5 size-3.5 shrink-0" />
            <span className="break-all">{worktree.path}</span>
          </p>
          <p
            className="mt-1 flex items-start gap-1.5 text-sm text-muted-foreground"
            data-testid={`${testId}-tooltip-branch`}
          >
            <GitBranchIcon aria-hidden className="mt-0.5 size-3.5 shrink-0" />
            <span className="break-all">{branchLabel}</span>
          </p>
          <p
            className="mt-1 flex items-center gap-1.5 text-sm text-muted-foreground"
            data-testid={`${testId}-tooltip-status`}
          >
            <StatusIcon aria-hidden className="size-3.5 shrink-0" />
            <span>{statusLabel}</span>
          </p>
        </TooltipContent>
      </Tooltip>
      {spacious && onOpen && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              className="ml-1 flex size-5 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
    </MenuItem>
  );
}
