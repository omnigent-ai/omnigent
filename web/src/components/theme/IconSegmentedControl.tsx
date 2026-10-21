import { type ComponentType, useRef } from "react";

import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { useOmnigentAnalytics } from "@/lib/analytics";
import { cn } from "@/lib/utils";

interface IconSegmentedOption<T extends string> {
  value: T;
  label: string;
  icon: ComponentType<{ className?: string }>;
  testId: string;
}

/** Compact icon-only radio group for closely related display choices. */
export function IconSegmentedControl<T extends string>({
  labelledBy,
  value,
  onSelect,
  componentId,
  items,
}: {
  labelledBy: string;
  value: T;
  onSelect: (value: T) => void;
  componentId?: string;
  items: readonly IconSegmentedOption<T>[];
}) {
  const { trackValueChange } = useOmnigentAnalytics();
  const refs = useRef(new Map<T, HTMLButtonElement | null>());
  const select = (next: T) => {
    if (componentId) {
      trackValueChange(componentId, "select", next, { valueHasNoPii: true });
    }
    onSelect(next);
  };

  return (
    <TooltipProvider>
      <div
        role="radiogroup"
        aria-labelledby={labelledBy}
        className="inline-flex shrink-0 gap-0.5 rounded-lg bg-muted p-0.5"
      >
        {items.map((item, index) => {
          const selected = item.value === value;
          const Icon = item.icon;
          return (
            <Tooltip key={item.value}>
              <TooltipTrigger asChild>
                <button
                  ref={(element) => {
                    refs.current.set(item.value, element);
                  }}
                  type="button"
                  role="radio"
                  aria-label={item.label}
                  aria-checked={selected}
                  tabIndex={selected ? 0 : -1}
                  data-testid={item.testId}
                  onClick={() => select(item.value)}
                  onKeyDown={(event) => {
                    const forward = event.key === "ArrowRight" || event.key === "ArrowDown";
                    const backward = event.key === "ArrowLeft" || event.key === "ArrowUp";
                    if (!forward && !backward) return;
                    event.preventDefault();
                    const nextIndex = (index + (forward ? 1 : -1) + items.length) % items.length;
                    const next = items[nextIndex].value;
                    select(next);
                    refs.current.get(next)?.focus();
                  }}
                  className={cn(
                    "flex size-9 items-center justify-center rounded-md text-muted-foreground transition-colors",
                    selected
                      ? "bg-background text-foreground shadow-sm ring-1 ring-border"
                      : "hover:bg-background/60 hover:text-foreground",
                  )}
                >
                  <Icon aria-hidden="true" className="size-4" />
                </button>
              </TooltipTrigger>
              <TooltipContent>{item.label}</TooltipContent>
            </Tooltip>
          );
        })}
      </div>
    </TooltipProvider>
  );
}
