import type { ReactNode } from "react";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

/** Keep hover and focus on a wrapper when the control itself is disabled. */
export function DisabledActionTooltip({
  reason,
  children,
  label,
}: {
  reason?: string;
  children: ReactNode;
  /** Gives native disabled buttons a keyboard-focusable tooltip target. */
  label?: string;
}) {
  if (!reason) return children;
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            tabIndex={label ? 0 : undefined}
            aria-label={label}
            aria-disabled={label ? true : undefined}
            className={
              label
                ? "inline-flex rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                : "block"
            }
          >
            {children}
          </span>
        </TooltipTrigger>
        <TooltipContent>{reason}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
