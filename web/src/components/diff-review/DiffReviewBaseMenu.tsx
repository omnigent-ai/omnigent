// What a review compares against, as a label, or as a menu when the source
// offers other bases to switch to. Unavailable bases stay listed, disabled,
// with the reason underneath.

import { ChevronDownIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

/** One base the person can switch to. */
export interface DiffReviewBaseChoice {
  id: string;
  label: string;
  /** A line under the label, such as what the choice compares with. */
  description?: string;
  checked: boolean;
  /** Why it can't be chosen here; null when it can. */
  unavailableReason: string | null;
  onSelect: () => void;
}

/** Marks the menu's trigger, so focus can follow it into a review that replaces this one. */
export const BASE_MENU_TRIGGER_ATTRIBUTE = "data-diff-review-base-menu";

/** What a review's files are compared against. */
export interface DiffReviewBase {
  /** Short, such as "vs HEAD". */
  label: string;
  /** The tooltip's longer explanation. */
  description: string;
  /** The menu's heading over the choices; "Compare changes with" by default. */
  heading?: string;
  /** Bases to switch to from the label's menu; absent for a fixed base. */
  choices?: readonly DiffReviewBaseChoice[];
  /** Actions after the choices, such as refreshing. */
  actions?: readonly { id: string; label: string; onSelect: () => void }[];
}

export function DiffReviewBaseMenu({
  base,
  className,
}: {
  base: DiffReviewBase;
  className?: string;
}) {
  if (!base.choices?.length) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            className={cn("truncate whitespace-nowrap text-muted-foreground", className)}
            data-testid="diff-review-base"
            aria-description={base.description}
          >
            {base.label}
          </span>
        </TooltipTrigger>
        <TooltipContent>{base.description}</TooltipContent>
      </Tooltip>
    );
  }
  const checked = base.choices.find((choice) => choice.checked)?.id ?? "";
  return (
    <DropdownMenu>
      <Tooltip>
        <TooltipTrigger asChild>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className={cn(
                "h-6 min-w-0 gap-1 px-1.5 font-normal text-muted-foreground hover:text-foreground",
                className,
              )}
              data-testid="diff-review-base"
              {...{ [BASE_MENU_TRIGGER_ATTRIBUTE]: "" }}
              aria-label={`Compared ${base.label}. Change what changes are compared with`}
              aria-description={base.description}
            >
              <span className="truncate">{base.label}</span>
              <ChevronDownIcon className="size-3 shrink-0" />
            </Button>
          </DropdownMenuTrigger>
        </TooltipTrigger>
        <TooltipContent>{base.description}</TooltipContent>
      </Tooltip>
      <DropdownMenuContent align="start" className="w-72">
        <DropdownMenuLabel>{base.heading ?? "Compare changes with"}</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={checked}>
          {base.choices.map((choice) => (
            <DropdownMenuRadioItem
              key={choice.id}
              value={choice.id}
              disabled={choice.unavailableReason !== null}
              onSelect={choice.onSelect}
              className="items-start"
            >
              {/* Lines under a choice wrap within the menu's width instead of running past it. */}
              <span className="flex min-w-0 flex-col">
                <span>{choice.label}</span>
                {choice.description && (
                  <span className="whitespace-normal text-xs text-muted-foreground">
                    {choice.description}
                  </span>
                )}
                {choice.unavailableReason && (
                  <span className="text-xs text-muted-foreground">{choice.unavailableReason}</span>
                )}
              </span>
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
        {base.actions?.length ? (
          <>
            <DropdownMenuSeparator />
            {base.actions.map((action) => (
              <DropdownMenuItem key={action.id} onSelect={action.onSelect}>
                {action.label}
              </DropdownMenuItem>
            ))}
          </>
        ) : null}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
