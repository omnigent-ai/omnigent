import { useId, type ComponentType, type ReactNode } from "react";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { DisabledActionTooltip } from "./DisabledActionTooltip";
import { cn } from "@/lib/utils";

interface ItemProps {
  children?: ReactNode;
  className?: string;
  onSelect?: (event: Event) => void;
  "aria-disabled"?: boolean;
  "aria-describedby"?: string;
  "data-testid"?: string;
}

/** Disabled menu actions stay in the arrow-key order so their reason can be read. */
export function SessionActionMenuItem({
  disabledReason,
  Item = DropdownMenuItem,
  onSelect,
  className,
  ...props
}: Omit<ItemProps, "aria-disabled"> & {
  disabledReason?: string;
  /** A Radix-compatible menu item that activates exclusively through onSelect. */
  Item?: ComponentType<ItemProps>;
}) {
  const descriptionId = useId();
  return (
    <DisabledActionTooltip reason={disabledReason}>
      <Item
        {...props}
        aria-disabled={disabledReason ? true : undefined}
        aria-describedby={
          [props["aria-describedby"], disabledReason && descriptionId].filter(Boolean).join(" ") ||
          undefined
        }
        className={cn(className, disabledReason && "cursor-not-allowed opacity-50")}
        onSelect={(event) => {
          if (disabledReason) event.preventDefault();
          else onSelect?.(event);
        }}
      />
      {disabledReason && (
        <span id={descriptionId} hidden>
          {disabledReason}
        </span>
      )}
    </DisabledActionTooltip>
  );
}
