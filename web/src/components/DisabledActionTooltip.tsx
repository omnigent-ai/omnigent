import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

/**
 * Keep hover and focus on a wrapper when the control itself is disabled.
 * The wrapper persists through capability loading so keyboard focus survives.
 */
export function DisabledActionTooltip({
  reason,
  children,
  label,
  className,
}: {
  reason?: string;
  children: ReactNode;
  /** Wraps one native button, handing focus to it when the reason clears. */
  label?: string;
  className?: string;
}) {
  const triggerRef = useRef<HTMLSpanElement>(null);
  const [open, setOpen] = useState(false);
  useLayoutEffect(() => {
    if (!reason) setOpen(false);
    if (!reason && label && document.activeElement === triggerRef.current) {
      triggerRef.current?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus();
    }
  }, [reason, label]);

  return (
    <TooltipProvider>
      <Tooltip open={!!reason && open} onOpenChange={(value) => setOpen(!!reason && value)}>
        <TooltipTrigger asChild>
          <span
            ref={triggerRef}
            role={reason && label ? "group" : "presentation"}
            tabIndex={reason && label ? 0 : undefined}
            aria-label={reason ? label : undefined}
            aria-disabled={reason && label ? true : undefined}
            className={cn(
              label
                ? "inline-flex rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
                : "block",
              reason && "[&_[disabled]]:pointer-events-none",
              className,
            )}
          >
            {children}
          </span>
        </TooltipTrigger>
        {reason && <TooltipContent>{reason}</TooltipContent>}
      </Tooltip>
    </TooltipProvider>
  );
}
