import { useId, type ComponentType, type ReactNode } from "react";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { DisabledActionTooltip } from "./DisabledActionTooltip";
import { cn } from "@/lib/utils";

interface ItemProps {
  asChild?: boolean;
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
  link,
  className,
  children,
  ...props
}: Omit<ItemProps, "aria-disabled" | "asChild"> & {
  disabledReason?: string;
  /** A Radix-compatible menu item that activates exclusively through onSelect. */
  Item?: ComponentType<ItemProps>;
  /**
   * Makes the row a link to `href`, which a click or Enter follows through `follow`;
   * `onSelect` doesn't run, so it opens once. A disabled row is never a link.
   */
  link?: { href: string; follow: (event: MouseEvent) => void };
}) {
  const descriptionId = useId();
  if (link && !disabledReason) {
    return (
      <Item {...props} className={className} asChild>
        <a
          href={link.href}
          target="_blank"
          rel="noopener noreferrer"
          // The native event: preventing it stops the browser following the link too,
          // while the menu, which skips closing for a click React saw prevented, still closes.
          onClick={(event) => link.follow(event.nativeEvent)}
        >
          {children}
        </a>
      </Item>
    );
  }
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
      >
        {children}
      </Item>
      {disabledReason && (
        <span id={descriptionId} hidden>
          {disabledReason}
        </span>
      )}
    </DisabledActionTooltip>
  );
}
