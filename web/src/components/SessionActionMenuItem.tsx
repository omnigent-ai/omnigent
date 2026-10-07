import type { ComponentType, ReactNode } from "react";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { DisabledActionTooltip } from "./DisabledActionTooltip";
import { cn } from "@/lib/utils";

interface ItemProps {
  children?: ReactNode;
  className?: string;
  onSelect?: (event: Event) => void;
  "aria-disabled"?: boolean;
  "aria-description"?: string;
  "data-testid"?: string;
}

/** Disabled menu actions stay in the arrow-key order so their reason can be read. */
export function SessionActionMenuItem({
  disabledReason,
  Item = DropdownMenuItem,
  onSelect,
  className,
  ...props
}: ItemProps & {
  disabledReason?: string;
  Item?: ComponentType<ItemProps>;
}) {
  return (
    <DisabledActionTooltip reason={disabledReason}>
      <Item
        {...props}
        aria-disabled={disabledReason ? true : undefined}
        aria-description={disabledReason}
        className={cn(className, disabledReason && "cursor-not-allowed opacity-50")}
        onSelect={(event) => {
          if (disabledReason) event.preventDefault();
          else onSelect?.(event);
        }}
      />
    </DisabledActionTooltip>
  );
}
