import type { ComponentProps } from "react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export function NeedsResponseBadge({
  children = "Needs response",
  className,
  ...props
}: ComponentProps<typeof Badge>) {
  return (
    <Badge {...props} className={cn("border-transparent bg-tag-pink text-brand-accent", className)}>
      {children}
    </Badge>
  );
}
