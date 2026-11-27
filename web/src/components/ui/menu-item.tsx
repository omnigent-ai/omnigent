import type { ComponentProps, ReactNode } from "react";
import * as Slot from "radix-ui/slot";

import { CompactShortcutKeys } from "@/components/ui/kbd";
import { type ShortcutId, shortcutAriaKeys, shortcutKeys } from "@/lib/shortcuts/registry";
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

/** Key caps at a menu row's end, in the command palette's style; touch screens hide them. */
export function MenuShortcut({ keys, className }: { keys: readonly string[]; className?: string }) {
  return (
    <CompactShortcutKeys
      keys={keys}
      className={cn("ml-auto shrink-0 pl-4 pointer-coarse:hidden", className)}
    />
  );
}

/**
 * A menu row's content followed by `shortcut`'s chord here, and the matching
 * `aria-keyshortcuts`. Rows without a chord here render their content unchanged.
 */
export function withMenuShortcut(
  children: ReactNode,
  shortcut: ShortcutId | undefined,
): { children: ReactNode; "aria-keyshortcuts"?: string } {
  const keys = shortcut ? shortcutKeys(shortcut) : [];
  if (!shortcut || keys.length === 0) return { children };
  return {
    children: (
      <>
        {children}
        <MenuShortcut keys={keys} />
      </>
    ),
    "aria-keyshortcuts": shortcutAriaKeys(shortcut),
  };
}

/** Adds an accessible description and optional shortcut to a menu row. */
export function describedWithShortcut(
  children: ReactNode,
  description: ReactNode,
  descriptionId: string,
  shortcut: ShortcutId | undefined,
): { children: ReactNode; "aria-describedby"?: string; "aria-keyshortcuts"?: string } {
  if (!description) return withMenuShortcut(children, shortcut);
  const described = (
    <span className="flex min-w-0 flex-1 flex-col gap-0.5">
      <span className="flex min-w-0 items-center gap-2">{children}</span>
      {/* Hidden from the row's name; aria-describedby still reads it. */}
      <span
        id={descriptionId}
        aria-hidden
        data-slot="menu-item-description"
        className="text-xs whitespace-normal text-muted-foreground"
      >
        {description}
      </span>
    </span>
  );
  return { ...withMenuShortcut(described, shortcut), "aria-describedby": descriptionId };
}
