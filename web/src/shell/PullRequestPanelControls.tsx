// Small controls shared by the Pull Requests panel's tabs.

import type { ReactNode } from "react";

import { Loader2Icon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

/** A centered status or empty-state message filling the panel body. */
export function PanelMessage({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center text-ui text-muted-foreground">
      {children}
    </div>
  );
}

/**
 * A subpage's state in the new pull request page, in the reference app's three shapes:
 * loading (a spinner and one line), empty (a title, a line, maybe an action) and failure
 * (a title read out as an alert, the reason, and how to recover).
 */
export function PanelStateMessage({
  variant,
  title,
  children,
  actions,
}: {
  variant: "loading" | "empty" | "failure";
  /** Absent for loading, which says what it's doing in one line. */
  title?: string;
  /** The line under the title, or the loading line. */
  children?: ReactNode;
  actions?: ReactNode;
}) {
  if (variant === "loading") {
    return (
      <PanelMessage>
        <span role="status" className="inline-flex items-center gap-2 text-xs">
          <Loader2Icon aria-hidden className="size-3.5 animate-spin" />
          {children}
        </span>
      </PanelMessage>
    );
  }
  return (
    <PanelMessage>
      <div
        role={variant === "failure" ? "alert" : "status"}
        className="flex max-w-sm flex-col items-center gap-1.5 text-xs"
      >
        {title && <strong className="text-sm font-semibold text-foreground">{title}</strong>}
        {children && <span className="break-words [overflow-wrap:anywhere]">{children}</span>}
      </div>
      {actions && <div className="mt-1.5 flex flex-wrap justify-center gap-2">{actions}</div>}
    </PanelMessage>
  );
}

/** A ghost icon button with a tooltip; the toolbar's shared control element.
 *  No-delay is set by the surrounding TooltipProvider. */
export function IconButton({
  label,
  onClick,
  disabled,
  className,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  className?: string;
  children: ReactNode;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="ghost"
          size="icon-xs"
          aria-label={label}
          onClick={onClick}
          disabled={disabled}
          className={cn("shrink-0", className)}
        >
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}
