import { CircleHelpIcon } from "lucide-react";
import { type ReactNode, useState } from "react";

import { cn } from "@/lib/utils";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

interface SettingsLabelProps {
  label: string;
  description: ReactNode;
  tooltipDescription?: ReactNode;
  labelId?: string;
  descriptionId?: string;
  className?: string;
  labelClassName?: string;
  descriptionClassName?: string;
  testId?: string;
  descriptionTestId?: string;
}

/** Keeps setting help inline on mobile while preserving desktop descriptions. */
export function SettingsLabel({
  label,
  description,
  tooltipDescription,
  labelId,
  descriptionId,
  className,
  labelClassName,
  descriptionClassName,
  testId,
  descriptionTestId,
}: SettingsLabelProps) {
  const [tooltipOpen, setTooltipOpen] = useState(false);

  return (
    <div className={cn("min-w-0", className)}>
      <div className="flex items-center gap-1.5">
        <span id={labelId} className={cn("text-ui font-normal md:font-medium", labelClassName)}>
          {label}
        </span>
        <TooltipProvider delayDuration={0}>
          <Tooltip open={tooltipOpen} onOpenChange={setTooltipOpen}>
            <TooltipTrigger asChild>
              <button
                type="button"
                aria-label={`About ${label}`}
                data-testid={testId}
                className="inline-flex size-5 shrink-0 items-center justify-center rounded-sm text-muted-foreground transition-colors hover:text-foreground md:hidden"
                onClick={(event) => {
                  event.preventDefault();
                  event.stopPropagation();
                  setTooltipOpen((open) => !open);
                }}
              >
                <CircleHelpIcon className="size-3.5" />
              </button>
            </TooltipTrigger>
            <TooltipContent
              side="top"
              align="start"
              className="max-w-[min(18rem,calc(100vw-2rem))] whitespace-normal leading-normal"
            >
              {tooltipDescription ?? description}
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>
      </div>
      <div
        id={descriptionId}
        data-testid={descriptionTestId}
        className={cn("text-sm text-muted-foreground max-md:hidden", descriptionClassName)}
      >
        {description}
      </div>
    </div>
  );
}
