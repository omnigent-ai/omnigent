// How deep a stacked pull request sits once its tree stops indenting, as the list, the stack
// menu and the description's stack card show it: its level's number, named "Level N".

import { cn } from "@/lib/utils";

/**
 * The theme tokens the number is drawn in, and on: the page's own color, so it reads as text on
 * a muted current row or a tinted checked one, where a muted layer of its own fell short.
 */
export const STACK_DEPTH_PILL_TOKENS = { text: "foreground", fill: "background" } as const;

export function StackDepthPill({ level, className }: { level: number; className?: string }) {
  const name = `Level ${level}`;
  return (
    <span
      role="img"
      aria-label={name}
      title={name}
      className={cn(
        "pr-stack-depth inline-flex h-4 min-w-4 shrink-0 items-center justify-center self-center rounded-full bg-background px-[3px] font-sans text-[10px] leading-none text-foreground tabular-nums ring-1 ring-border ring-inset",
        className,
      )}
    >
      {level}
    </span>
  );
}
