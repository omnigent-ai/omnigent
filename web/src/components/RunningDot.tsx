import { Loader2Icon } from "lucide-react";
import { cn } from "@/lib/utils";

export function RunningDot({ className }: { className?: string }) {
  return (
    <Loader2Icon
      aria-hidden
      role="presentation"
      data-testid="running-dot"
      className={cn("size-3 shrink-0 animate-spin text-muted-foreground", className)}
    />
  );
}
