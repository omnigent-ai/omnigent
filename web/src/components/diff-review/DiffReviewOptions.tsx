// The toolbar's "Diff options" popover: the less common view choices, each run through the
// same command ⌘K offers, so the two can't drift apart.

import { SlidersHorizontalIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { type DiffReviewCommand, WRAP_LINES_SETTING_LABEL } from "./diffReviewCommands";

export const DIFF_OPTIONS_LABEL = "Diff options";

/** How files are sorted, and whether long lines wrap, where the review offers each. */
export function DiffReviewOptions({
  sort,
  wrap,
}: {
  sort: DiffReviewCommand | undefined;
  wrap: DiffReviewCommand | undefined;
}) {
  const sorting = sort?.setting;
  if (!sorting && !wrap) return null;
  return (
    <DropdownMenu>
      <Tooltip>
        <TooltipTrigger asChild>
          <DropdownMenuTrigger asChild>
            <Button type="button" variant="ghost" size="icon-sm" aria-label={DIFF_OPTIONS_LABEL}>
              <SlidersHorizontalIcon />
            </Button>
          </DropdownMenuTrigger>
        </TooltipTrigger>
        <TooltipContent>{DIFF_OPTIONS_LABEL}</TooltipContent>
      </Tooltip>
      <DropdownMenuContent align="end" aria-label={DIFF_OPTIONS_LABEL} className="w-56">
        {sorting && (
          <>
            <DropdownMenuLabel>Sort files</DropdownMenuLabel>
            <DropdownMenuRadioGroup value={sorting.current} onValueChange={sorting.choose}>
              {sorting.choices.map((choice) => (
                <DropdownMenuRadioItem key={choice.value} value={choice.value}>
                  {choice.label}
                </DropdownMenuRadioItem>
              ))}
            </DropdownMenuRadioGroup>
          </>
        )}
        {sorting && wrap && <DropdownMenuSeparator />}
        {wrap && (
          <DropdownMenuCheckboxItem
            checked={wrap.pressed ?? false}
            onCheckedChange={(checked) => {
              if (checked !== wrap.pressed) wrap.run();
            }}
          >
            {WRAP_LINES_SETTING_LABEL}
          </DropdownMenuCheckboxItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
