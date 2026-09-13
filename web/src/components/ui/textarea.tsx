import * as React from "react";

import { cn } from "@/lib/utils";
import { useOmnigentAnalytics } from "@/lib/analytics";

function Textarea({
  className,
  componentId,
  onChange,
  ...props
}: React.ComponentProps<"textarea"> & {
  // Opt-in analytics id. When set, a value-change is reported to the host sink
  // (see `lib/analytics.ts`). The value itself is redacted by default (textarea
  // text is treated as PII); only that the field changed is sent.
  componentId?: string;
}) {
  const { trackValueChange } = useOmnigentAnalytics();
  const handleChange = componentId
    ? (e: React.ChangeEvent<HTMLTextAreaElement>) => {
        trackValueChange(componentId, "textarea");
        onChange?.(e);
      }
    : onChange;
  return (
    <textarea
      data-slot="textarea"
      data-component-id={componentId}
      onChange={handleChange}
      className={cn(
        "flex field-sizing-content min-h-16 w-full rounded-lg border border-input bg-transparent px-2.5 py-2 text-ui transition-colors outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:bg-input/50 disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20 md:text-ui dark:bg-input/30 dark:disabled:bg-input/80 dark:aria-invalid:border-destructive/50 dark:aria-invalid:ring-destructive/40",
        className,
      )}
      {...props}
    />
  );
}

export { Textarea };
