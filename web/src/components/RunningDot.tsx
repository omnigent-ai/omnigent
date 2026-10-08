import { Loader2Icon } from "lucide-react";
import { cn } from "@/lib/utils";

// Spin the HTML wrapper, not the svg: Chrome composites SVG transform animations
// only at effective zoom 1, so on HiDPI an svg spin runs on the main thread.
// Center the svg so it still spins in place when CSS resizes it (mobile sidebar).
export function RunningDot({ className }: { className?: string }) {
  return (
    <span
      aria-hidden
      role="presentation"
      data-testid="running-dot"
      className={cn(
        "flex size-3 shrink-0 animate-spin items-center justify-center text-muted-foreground",
        className,
      )}
    >
      <Loader2Icon className="size-full" />
    </span>
  );
}
