// Menu rows with a quieter line under the label, such as why a disabled action can't run.

import { type ComponentProps, type ReactNode, useId } from "react";

import { DropdownMenuItem, DropdownMenuRadioItem } from "@/components/ui/dropdown-menu";
import { describedWithShortcut } from "@/components/ui/menu-item";

/**
 * Width for a menu whose rows carry descriptions: wide enough that labels don't truncate, with
 * the descriptions wrapping, and never wider than the space beside the trigger.
 */
export const DESCRIBED_MENU_CONTENT_CLASS =
  "min-w-72 max-w-[min(24rem,var(--radix-dropdown-menu-content-available-width))]";

interface Described {
  /** Read as the row's description, not part of its name. */
  description?: ReactNode;
}

/** A menu row with an optional description under its label. */
export function DescribedMenuItem({
  description,
  children,
  ...props
}: ComponentProps<typeof DropdownMenuItem> & Described) {
  const descriptionId = useId();
  return (
    <DropdownMenuItem
      {...props}
      {...describedWithShortcut(children, description, descriptionId, undefined)}
    />
  );
}

/** A radio choice with an optional description saying what it does. */
export function DescribedMenuRadioItem({
  description,
  children,
  ...props
}: ComponentProps<typeof DropdownMenuRadioItem> & Described) {
  const descriptionId = useId();
  return (
    <DropdownMenuRadioItem
      {...props}
      {...describedWithShortcut(children, description, descriptionId, undefined)}
    />
  );
}
