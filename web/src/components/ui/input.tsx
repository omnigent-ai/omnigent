import * as React from "react";

import { cn } from "@/lib/utils";
import { useOmnigentAnalytics } from "@/lib/analytics";

const Input = React.forwardRef<
  HTMLInputElement,
  React.ComponentProps<"input"> & {
    // Opt-in analytics id. When set, a value-change is reported to the host
    // sink (see `lib/analytics.ts`). The value itself is redacted by default
    // (input text is treated as PII); only that the field changed is sent.
    componentId?: string;
  }
>(({ className, type, componentId, onChange, ...props }, ref) => {
  const { trackValueChange } = useOmnigentAnalytics();
  const handleChange = componentId
    ? (e: React.ChangeEvent<HTMLInputElement>) => {
        trackValueChange(componentId, "input");
        onChange?.(e);
      }
    : onChange;
  return (
    <input
      ref={ref}
      type={type}
      data-slot="input"
      data-component-id={componentId}
      onChange={handleChange}
      className={cn(
        "h-8 w-full min-w-0 rounded-lg border border-input bg-transparent px-2.5 py-1 text-ui transition-colors outline-none file:inline-flex file:h-6 file:border-0 file:bg-transparent file:text-ui file:font-medium file:text-foreground placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:cursor-not-allowed disabled:bg-input/50 disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20 md:text-ui dark:bg-input/30 dark:disabled:bg-input/80 dark:aria-invalid:border-destructive/50 dark:aria-invalid:ring-destructive/40",
        className,
      )}
      {...props}
    />
  );
});

Input.displayName = "Input";

export { Input };
