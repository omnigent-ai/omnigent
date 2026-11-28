import * as React from "react";
import * as RadioGroupPrimitive from "radix-ui/radio-group";

import { cn } from "@/lib/utils";

/** A set of choices where one is picked; Radix owns roving focus, arrow keys and ARIA. */
function RadioGroup({
  className,
  ...props
}: React.ComponentProps<typeof RadioGroupPrimitive.Root>) {
  return (
    <RadioGroupPrimitive.Root
      data-slot="radio-group"
      className={cn("grid gap-2", className)}
      {...props}
    />
  );
}

function RadioGroupItem({
  className,
  ...props
}: React.ComponentProps<typeof RadioGroupPrimitive.Item>) {
  return (
    <RadioGroupPrimitive.Item
      data-slot="radio-group-item"
      className={cn(
        "peer relative flex size-4 shrink-0 cursor-pointer items-center justify-center rounded-full border border-input bg-background transition-colors outline-none after:absolute after:-inset-x-3 after:-inset-y-2 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 dark:bg-input data-[state=checked]:border-primary data-disabled:cursor-not-allowed data-disabled:opacity-50",
        className,
      )}
      {...props}
    >
      <RadioGroupPrimitive.Indicator
        data-slot="radio-group-indicator"
        className="size-2 rounded-full bg-primary"
      />
    </RadioGroupPrimitive.Item>
  );
}

/**
 * A choice drawn as its own content, such as an icon or a colour, outlined while picked; Radix
 * still owns roving focus, arrow keys and ARIA. Give it an `aria-label` naming the choice.
 */
function RadioGroupSwatch({
  className,
  ...props
}: React.ComponentProps<typeof RadioGroupPrimitive.Item>) {
  return (
    <RadioGroupPrimitive.Item
      data-slot="radio-group-swatch"
      className={cn(
        "flex size-7 shrink-0 cursor-pointer items-center justify-center rounded-md border border-transparent text-muted-foreground transition-colors outline-none hover:bg-muted hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 data-[state=checked]:border-brand-accent data-[state=checked]:bg-muted data-[state=checked]:text-foreground data-disabled:cursor-not-allowed data-disabled:opacity-50 [&_svg]:size-4",
        className,
      )}
      {...props}
    />
  );
}

export { RadioGroup, RadioGroupItem, RadioGroupSwatch };
