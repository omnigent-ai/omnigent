import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export function CompactKbd({ children }: { children: ReactNode }) {
  return (
    <kbd
      data-slot="kbd"
      className="inline-flex size-4 items-center justify-center rounded border border-border/80 bg-muted px-1 text-10 font-medium text-muted-foreground"
    >
      {children}
    </kbd>
  );
}

export function CompactShortcutKeys({
  keys,
  className,
}: {
  keys: readonly string[];
  className?: string;
}) {
  return (
    <span
      aria-hidden="true"
      data-slot="shortcut-keys"
      className={cn("flex items-center gap-0.5 whitespace-nowrap", className)}
    >
      {keys.map((key) => (
        <CompactKbd key={key}>{key}</CompactKbd>
      ))}
    </span>
  );
}
