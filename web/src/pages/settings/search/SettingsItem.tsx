// The row a catalog entry renders as: the element search scrolls to and marks, and the
// label its controls are named by.

import { type ComponentProps, type ReactNode, type RefObject, useEffect, useRef } from "react";

import { SettingsLabel } from "@/components/SettingsLabel";
import { useLocation } from "@/lib/routing";
import { cn } from "@/lib/utils";

import type { SettingsEntry } from "./catalog/types";
import { parseSettingsHash, settingsDescriptionId, settingsLabelId } from "./settingsControl";
import {
  noteSettingsNavigation,
  registerSettingsAnchor,
  revealSettingsRow,
  SETTINGS_HIGHLIGHT_CLASS,
} from "./settingsReveal";

interface SettingsItemProps extends Omit<ComponentProps<"div">, "children"> {
  entry: SettingsEntry;
  children: ReactNode;
}

/** `entry`'s row. */
export function SettingsItem({ entry, children, className, ...rest }: SettingsItemProps) {
  const rowRef = useRef<HTMLDivElement | null>(null);
  useSettingsRowReveal(entry, rowRef);
  return (
    <div
      ref={rowRef}
      data-settings-item={entry.id}
      className={cn(SETTINGS_HIGHLIGHT_CLASS, className)}
      {...rest}
    >
      {children}
    </div>
  );
}

/** Starts each navigation's reveal window; mounted by the Settings page, before any row. */
export function useSettingsNavigationClock() {
  const location = useLocation();
  useEffect(() => {
    noteSettingsNavigation(location);
  }, [location]);
}

/** Reveals the row in `rowRef` when the URL names `entry`; a newer navigation cancels it. */
function useSettingsRowReveal(entry: SettingsEntry, rowRef: RefObject<HTMLElement | null>) {
  // A new location object per navigation, so the same hash chosen again, or reached by Back,
  // reveals again.
  const location = useLocation();
  useEffect(() => registerSettingsAnchor(entry.id), [entry.id]);
  useEffect(() => {
    const target = parseSettingsHash(location.hash);
    const row = rowRef.current;
    if (!target || !row) return;
    const controller = new AbortController();
    revealSettingsRow({ row, entry, target, location, signal: controller.signal });
    return () => controller.abort();
  }, [entry, location, rowRef]);
}

type SettingsEntryLabelProps = Omit<
  ComponentProps<typeof SettingsLabel>,
  "label" | "labelId" | "descriptionId" | "description"
> & {
  entry: SettingsEntry;
  /** A richer description than the catalog's plain text, such as one with the key glyphs. */
  description?: ReactNode;
};

/** The row's label and description, with the ids its main control is labelled by. */
export function SettingsEntryLabel({ entry, description, ...rest }: SettingsEntryLabelProps) {
  return (
    <SettingsLabel
      label={entry.label}
      labelId={settingsLabelId(entry)}
      descriptionId={settingsDescriptionId(entry)}
      description={description ?? entry.description}
      {...rest}
    />
  );
}
