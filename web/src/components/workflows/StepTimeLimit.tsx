import { useEffect, useRef } from "react";
import { TimerIcon } from "lucide-react";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  shortTimeLimit,
  stepTimeLimit,
  TIME_LIMIT_CHOICES,
  timeLimitLength,
  timeLimitMinutes,
} from "@/lib/stepTimeLimit";
import { cn } from "@/lib/utils";

/** A time limit chosen in a step's menu. */
export interface StepTimeLimitChange {
  stepId: string;
  /** e.g. "2 hours"; null for no limit. */
  limit: string | null;
}

const NO_LIMIT = "none";
const CHIP =
  "inline-flex shrink-0 items-center gap-1 rounded-full border border-border px-1.5 text-xs leading-[18px] text-muted-foreground tabular-nums";
// React Flow leaves a press on these alone rather than panning.
const BUTTON =
  "nodrag nopan cursor-pointer hover:bg-foreground/[0.06] hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 disabled:cursor-default disabled:opacity-50";

/**
 * A step's time limit on its box, from the Time limit line in its description. Given
 * `onChange`, a menu sets it, and a step without one shows a button to add one.
 */
export function StepTimeLimit({
  stepId,
  stepName,
  goal,
  onChange,
  disabled = false,
  onOpenChange,
}: {
  stepId: string;
  stepName: string;
  goal: string | null;
  onChange?: (change: StepTimeLimitChange) => void;
  disabled?: boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  const openRef = useRef(false);
  const onOpenChangeRef = useRef(onOpenChange);
  onOpenChangeRef.current = onOpenChange;
  // The step's hover card waits while the menu is open; tell it the menu went if it unmounts open.
  useEffect(
    () => () => {
      if (openRef.current) onOpenChangeRef.current?.(false);
    },
    [],
  );
  const limit = stepTimeLimit(goal);
  const short = limit ? shortTimeLimit(limit) : null;
  // A limit worded at more length, e.g. "2 hours. Tell me after.", goes by its length.
  const length = limit ? (timeLimitLength(limit) ?? limit) : null;
  const icon = <TimerIcon aria-hidden className="size-3" />;
  if (!onChange) {
    if (!limit) return null;
    return (
      <span className={CHIP} title={`Time limit: ${limit}`} data-testid="workflow-step-time-limit">
        {icon}
        {short}
      </span>
    );
  }
  const minutes = limit ? timeLimitMinutes(limit) : null;
  const choice = limit
    ? (TIME_LIMIT_CHOICES.find((option) => timeLimitMinutes(option) === minutes) ?? "")
    : NO_LIMIT;
  const label = length
    ? `Time limit for ${stepName}: ${length}`
    : `Set a time limit for ${stepName}`;
  return (
    <DropdownMenu
      onOpenChange={(open) => {
        openRef.current = open;
        onOpenChange?.(open);
      }}
    >
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={label}
          title={label}
          disabled={disabled}
          className={cn(
            BUTTON,
            limit
              ? CHIP
              : "-my-0.5 shrink-0 rounded-md p-1 text-muted-foreground/70 [&_svg]:size-3.5",
          )}
          data-testid="workflow-step-time-limit"
        >
          {icon}
          {short}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>Time limit</DropdownMenuLabel>
        <DropdownMenuRadioGroup
          value={choice}
          onValueChange={(value) => {
            // Picking the checked option again would rewrite a limit worded at more length.
            if (value !== choice) onChange({ stepId, limit: value === NO_LIMIT ? null : value });
          }}
        >
          <DropdownMenuRadioItem value={NO_LIMIT}>No limit</DropdownMenuRadioItem>
          {TIME_LIMIT_CHOICES.map((option) => (
            <DropdownMenuRadioItem key={option} value={option}>
              {option}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
