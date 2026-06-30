import { Loader2Icon } from "lucide-react";

export function RunningDot() {
  return (
    <Loader2Icon
      aria-hidden
      role="presentation"
      data-testid="running-dot"
      className="size-3 shrink-0 animate-spin text-muted-foreground"
    />
  );
}
