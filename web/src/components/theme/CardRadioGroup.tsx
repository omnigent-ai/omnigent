import { useRef, type ReactNode } from "react";
import { CheckIcon } from "lucide-react";
import { useOmnigentAnalytics } from "@/lib/analytics";
import { cn } from "@/lib/utils";

export interface CardRadioOption<T extends string> {
  value: T;
  testId: string;
  body: ReactNode;
  title?: string;
}

export function CardRadioGroup<T extends string>({
  labelledBy,
  value,
  onSelect,
  componentId,
  items,
  className,
  cardClassName,
}: {
  labelledBy: string;
  value: T;
  onSelect: (value: T) => void;
  componentId?: string;
  items: readonly CardRadioOption<T>[];
  className?: string;
  cardClassName?: string;
}) {
  const { trackValueChange } = useOmnigentAnalytics();
  const select = componentId
    ? (next: T) => {
        trackValueChange(componentId, "select", next, { valueHasNoPii: true });
        onSelect(next);
      }
    : onSelect;
  const refs = useRef(new Map<T, HTMLButtonElement | null>());

  return (
    <div role="radiogroup" aria-labelledby={labelledBy} className={className}>
      {items.map((item, index) => {
        const selected = item.value === value;
        return (
          <button
            key={item.value}
            ref={(element) => {
              refs.current.set(item.value, element);
            }}
            type="button"
            role="radio"
            aria-checked={selected}
            tabIndex={selected ? 0 : -1}
            title={item.title}
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
            className={themeCardClass(selected, cardClassName)}
          >
            {selected && <SelectedBadge />}
            {item.body}
          </button>
        );
      })}
    </div>
  );
}

function SelectedBadge() {
  return (
    <span
      aria-hidden
      className="absolute right-1.5 top-1.5 flex size-4 items-center justify-center rounded-full bg-primary text-primary-foreground shadow-sm"
    >
      <CheckIcon className="size-3" />
    </span>
  );
}

function themeCardClass(selected: boolean, layout?: string) {
  return cn(
    "relative flex flex-col rounded-lg border-2 transition-[color,background-color,border-color,box-shadow]",
    selected
      ? "border-primary bg-primary/5"
      : "border-border hover:border-border-strong hover:bg-muted hover:shadow-sm",
    layout,
  );
}
