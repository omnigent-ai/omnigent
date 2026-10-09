import type { ComponentProps } from "react";
import * as Slot from "radix-ui/slot";

import { cn } from "@/lib/utils";

export interface MenuItemProps extends ComponentProps<"div"> {
  active?: boolean;
  asChild?: boolean;
  density?: "default" | "compact" | "none";
  interactive?: boolean;
}

/** Shared DS row surface for menus, pickers, and selector lists. */
export function MenuItem({
  active = false,
  asChild = false,
  density = "default",
  interactive = true,
  className,
  ...props
}: MenuItemProps) {
  const Comp = asChild ? Slot.Root : "div";

  return (
    <Comp
      data-slot="menu-item"
      data-density={density}
      data-active={active || undefined}
      className={cn(
        "relative flex min-w-0 items-center gap-2 rounded-md text-ui outline-hidden select-none transition-colors",
        density === "default" && "px-1.5 py-1",
        density === "compact" && "h-7 px-2 py-[3px] leading-4",
        interactive && "cursor-pointer hover:bg-muted focus:bg-muted",
        !interactive && "cursor-default",
        active && "bg-muted",
        className,
      )}
      {...props}
    />
  );
}
