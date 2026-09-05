import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

export function GoalActivityBadge({ state }: { state: "active" | "paused" }) {
  const active = state === "active";
  const label = active ? "Goal active" : "Goal paused";
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          data-testid="goal-activity-badge"
          data-state={state}
          role="img"
          aria-label={label}
          className={cn(
            "inline-flex size-4 shrink-0 items-center justify-center rounded border font-semibold text-[10px] leading-none",
            active
              ? "border-status-green/55 bg-status-green/10 text-status-green"
              : "border-status-yellow/55 bg-status-yellow/10 text-status-yellow",
          )}
        >
          G
        </span>
      </TooltipTrigger>
      <TooltipContent side="left">{label}</TooltipContent>
    </Tooltip>
  );
}
