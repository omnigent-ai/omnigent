import * as React from "react";
import * as CheckboxPrimitive from "radix-ui/checkbox";
import { CheckIcon, MinusIcon } from "lucide-react";

import { cn } from "@/lib/utils";
import { useOmnigentAnalytics } from "@/lib/analytics";

function Checkbox({
  className,
  componentId,
  onCheckedChange,
  ...props
}: React.ComponentProps<typeof CheckboxPrimitive.Root> & {
  // Opt-in analytics id. When set, a toggle reports the new checked state to the
  // host sink (see `lib/analytics.ts`). A boolean carries no PII, so it's sent.
  componentId?: string;
}) {
  const { trackValueChange } = useOmnigentAnalytics();
  const handleCheckedChange = componentId
    ? (checked: CheckboxPrimitive.CheckedState) => {
        trackValueChange(componentId, "checkbox", checked === true, { valueHasNoPii: true });
        onCheckedChange?.(checked);
      }
    : onCheckedChange;
  return (
    <CheckboxPrimitive.Root
      data-slot="checkbox"
      data-component-id={componentId}
      onCheckedChange={handleCheckedChange}
      className={cn(
        "group/checkbox peer relative flex size-4 shrink-0 cursor-pointer items-center justify-center rounded-[4px] border border-input bg-background transition-colors outline-none after:absolute after:-inset-x-3 after:-inset-y-2 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20 dark:bg-input/30 dark:aria-invalid:border-destructive/50 dark:aria-invalid:ring-destructive/40 data-checked:border-primary data-checked:bg-primary data-checked:text-primary-foreground dark:data-checked:bg-primary data-[state=indeterminate]:border-primary data-[state=indeterminate]:bg-primary data-[state=indeterminate]:text-primary-foreground data-disabled:cursor-not-allowed data-disabled:opacity-50",
        className,
      )}
      {...props}
    >
      <CheckboxPrimitive.Indicator
        data-slot="checkbox-indicator"
        className="grid place-content-center text-current"
      >
        {/* Driven by Radix's own state so uncontrolled checkboxes show the right glyph. */}
        <CheckIcon
          className="size-3 group-data-[state=indeterminate]/checkbox:hidden"
          strokeWidth={3}
        />
        <MinusIcon
          className="hidden size-3 group-data-[state=indeterminate]/checkbox:block"
          strokeWidth={3}
        />
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  );
}

export { Checkbox };
