import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

interface SettingsGroupProps {
  title: string;
  children: ReactNode;
  className?: string;
  contentClassName?: string;
  testId?: string;
}

/** General-style Settings group: subsection title above one outlined card. */
export function SettingsGroup({
  title,
  children,
  className,
  contentClassName,
  testId,
}: SettingsGroupProps) {
  return (
    <section className={cn("flex flex-col gap-3", className)} data-testid={testId}>
      <h2 className="text-ui font-medium">{title}</h2>
      <div className={cn("rounded-xl border border-border bg-card p-4", contentClassName)}>
        {children}
      </div>
    </section>
  );
}
