// Floating ↑↓ controls peer to the scroll-to-bottom button. Opposite
// corner (right-4) so the two don't collide.

import { ChevronDownIcon, ChevronUpIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

export interface UserMessageNavProps {
  goPrev: () => void;
  goNext: () => void;
  canPrev: boolean;
  canNext: boolean;
  /** Hide entirely. Parent sets this when there are no user messages. */
  hidden: boolean;
  className?: string;
}

export function UserMessageNav({
  goPrev,
  goNext,
  canPrev,
  canNext,
  hidden,
  className,
}: UserMessageNavProps) {
  if (hidden) return null;
  return (
    <TooltipProvider>
      <div
        className={cn(
          "pointer-events-none absolute right-4 bottom-4 flex flex-col gap-1",
          className,
        )}
      >
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              className="pointer-events-auto rounded-full dark:bg-background dark:hover:bg-muted"
              onClick={goPrev}
              disabled={!canPrev}
              size="icon"
              type="button"
              variant="outline"
              aria-label="Previous user message"
            >
              <ChevronUpIcon className="size-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="left">Previous message (⌘⌥↑)</TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              className="pointer-events-auto rounded-full dark:bg-background dark:hover:bg-muted"
              onClick={goNext}
              disabled={!canNext}
              size="icon"
              type="button"
              variant="outline"
              aria-label="Next user message"
            >
              <ChevronDownIcon className="size-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="left">Next message (⌘⌥↓)</TooltipContent>
        </Tooltip>
      </div>
    </TooltipProvider>
  );
}
