// A command row that is one of several choices to check, such as a label or a reviewer.

import { CheckIcon } from "lucide-react";
import type { ComponentProps } from "react";

import { CommandItem } from "@/components/ui/command";
import { cn } from "@/lib/utils";

/** A command row led by a checkbox showing whether it's chosen. */
export function CheckboxCommandItem({
  checked,
  children,
  ...props
}: ComponentProps<typeof CommandItem> & { checked: boolean }) {
  return (
    <CommandItem {...props} aria-checked={checked}>
      <span
        aria-hidden
        data-slot="command-item-checkbox"
        className={cn(
          "flex size-4 shrink-0 items-center justify-center rounded-[4px] border border-input bg-background text-primary-foreground dark:bg-input",
          checked && "border-primary bg-primary dark:bg-primary",
        )}
      >
        <CheckIcon
          strokeWidth={3}
          className={cn(
            "size-3! group-data-selected/command-item:text-primary-foreground!",
            !checked && "opacity-0",
          )}
        />
      </span>
      {children}
    </CommandItem>
  );
}
