// A view's sections as tabs, as the PR page and the Diffs view show them: each at its label's
// width with an icon and an optional count, and one underline that slides to the shown tab. The
// live row and its passive measuring copy share one face, class and stylesheet scope.

import type { LucideIcon } from "lucide-react";
import { type ComponentProps, useLayoutEffect, useRef } from "react";

import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  TabsList,
  TabsTrigger,
  tabsListVariants,
  tabsTriggerClassName,
} from "@/components/ui/tabs";
import { cn } from "@/lib/utils";

import { useSlidingTabIndicator } from "./useSlidingTabIndicator";
import "./sectionTabs.css";

/** What a narrow pane drops from each tab. */
export type SectionTabsNarrow = "drop-icons" | "drop-labels";

export interface SectionTab<Id extends string = string> {
  id: Id;
  label: string;
  icon: LucideIcon;
  /** Shown as a pill once known; a lower bound when `partial`. */
  count?: number;
  partial?: boolean;
}

/**
 * A section's count as an accent-tinted pill that stands out on the tab strip and on the
 * selected tab alike. Nothing shows while it's unknown; a known zero shows 0, a lower bound a +.
 */
export function TabCount({ count, partial = false }: { count?: number; partial?: boolean }) {
  if (count === undefined || (count === 0 && partial)) return null;
  return (
    <Badge
      variant="secondary"
      className="h-4 min-w-4 border-0 bg-brand-accent/22 px-1 py-0 text-xs leading-4 text-foreground tabular-nums"
    >
      {count}
      {partial ? "+" : ""}
    </Badge>
  );
}

const SECTION_TABS_VARIANT = "line" as const;

/** The padding and weights each tab gets, shared with its measuring copy. */
const SECTION_TAB_CLASS = cn(
  "flex-none px-2 text-muted-foreground dark:text-muted-foreground",
  "data-active:font-semibold data-active:text-foreground dark:data-active:text-foreground",
  "after:bg-brand-accent",
);

/** A section's tab face: its icon, label and count, the same in the tab and its measure. */
function SectionTabFace({ tab }: { tab: SectionTab }) {
  const Icon = tab.icon;
  return (
    <>
      <Icon aria-hidden data-section-icon className="size-3.5" />
      {/* The space sits outside the label so the tab's name reads "Commits 2", not "Commits2". */}
      <span data-section-label="">{tab.label}</span>{" "}
      <TabCount count={tab.count} partial={tab.partial} />
    </>
  );
}

/**
 * The live tabs, inside a Radix `Tabs` that owns focus, keys and selection. Extra props go to the
 * tab list, such as `aria-label`, `hidden` or a caller's own data attributes.
 */
export function SectionTabs({
  tabs,
  disabled = false,
  narrow = "drop-icons",
  className,
  ...listProps
}: {
  tabs: readonly SectionTab[];
  /** In their places but not yet open. */
  disabled?: boolean;
  /** What a narrow pane drops: the icons, or the labels so each tab is its icon. */
  narrow?: SectionTabsNarrow;
} & Omit<ComponentProps<typeof TabsList>, "children" | "variant">) {
  // The slider finds the tab list inside this wrapper, which takes no box of its own.
  const containerRef = useRef<HTMLDivElement>(null);
  useSlidingTabIndicator(containerRef);
  return (
    <div ref={containerRef} className="contents">
      <TabsList
        variant={SECTION_TABS_VARIANT}
        data-section-tabs=""
        data-narrow={narrow}
        className={cn("relative", className)}
        {...listProps}
      >
        {tabs.map((tab) => (
          <TabsTrigger
            key={tab.id}
            value={tab.id}
            disabled={disabled}
            // Where a narrow pane leaves only the icon, its label still shows on hover.
            title={narrow === "drop-labels" ? tab.label : undefined}
            className={SECTION_TAB_CLASS}
          >
            <SectionTabFace tab={tab} />
          </TabsTrigger>
        ))}
        {/* The shown section's underline, sliding between tabs; drawn only. */}
        <span aria-hidden data-tab-indicator className="bg-brand-accent" />
      </TabsList>
    </div>
  );
}

/**
 * A passive copy of the tabs for measuring, laid out as the live ones are, at the shown tab's
 * weight so the bolder one always fits. Plain spans: no roles, ids, focus or observers.
 */
export function SectionTabsMeasure({
  tabs,
  narrow = "drop-icons",
  ...props
}: { tabs: readonly SectionTab[]; narrow?: SectionTabsNarrow } & Omit<
  ComponentProps<"span">,
  "children" | "className"
>) {
  return (
    <span
      data-section-tabs=""
      data-narrow={narrow}
      className={tabsListVariants({ variant: SECTION_TABS_VARIANT })}
      {...props}
    >
      {tabs.map((tab) => (
        <span key={tab.id} className={tabsTriggerClassName(cn(SECTION_TAB_CLASS, "font-semibold"))}>
          <SectionTabFace tab={tab} />
        </span>
      ))}
    </span>
  );
}

/** Marks the picker standing in for folded tabs, so focus can find it. */
const SECTION_PICKER = "data-section-picker";

/**
 * Focus the control that shows the current section: its tab, or the picker standing in for
 * the tabs. False when neither is on screen, so the caller can fall back.
 */
export function focusSectionControl(root: ParentNode | null | undefined): boolean {
  const control =
    root?.querySelector<HTMLElement>(`[${SECTION_PICKER}]`) ??
    root?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]');
  if (!control || control.closest("[hidden]")) return false;
  control.focus();
  return document.activeElement === control;
}

/**
 * The sections as tabs, or, where the caller says they don't fit, a picker showing the current
 * one. The tabs stay mounted behind the picker, hidden, so each panel keeps its accessible name,
 * and focus in either moves to the other as they swap.
 */
export function FoldingSectionTabs<Id extends string>({
  tabs,
  shown,
  select,
  folded,
  label,
  pickerLabel,
  disabled = false,
  narrow,
  className,
  listProps,
  pickerProps,
}: {
  tabs: readonly SectionTab<Id>[];
  shown: Id;
  select: (id: Id) => void;
  /** The tabs don't fit: the picker shows instead. */
  folded: boolean;
  /** The tab list's accessible name. */
  label: string;
  /** The picker's accessible name. */
  pickerLabel: string;
  disabled?: boolean;
  narrow?: SectionTabsNarrow;
  /** The wrapper's class, which lays out the row. */
  className?: string;
  /** Extra attributes for the tab list and for the picker, such as a caller's data markers. */
  listProps?: Record<`data-${string}`, string>;
  pickerProps?: Record<`data-${string}`, string>;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  // Whether focus, or the picker's open list, belongs to the navigation: a swap keeps it there.
  const ownsFocus = useRef(false);
  const pickerOpen = useRef(false);
  const shownFold = useRef(folded);
  useLayoutEffect(() => {
    if (shownFold.current === folded) return;
    shownFold.current = folded;
    const hadFocus = ownsFocus.current || pickerOpen.current;
    pickerOpen.current = false;
    if (hadFocus) focusSectionControl(containerRef.current);
  }, [folded]);
  return (
    <div
      ref={containerRef}
      className={className}
      onFocus={() => {
        ownsFocus.current = true;
      }}
      onBlur={(event) => {
        const next = event.relatedTarget;
        ownsFocus.current = next instanceof Node && !!containerRef.current?.contains(next);
      }}
    >
      <SectionTabs
        tabs={tabs}
        disabled={disabled}
        narrow={narrow}
        aria-label={label}
        hidden={folded}
        {...listProps}
      />
      {folded && (
        <Select
          value={shown}
          disabled={disabled}
          onValueChange={(value) => select(value as Id)}
          onOpenChange={(open) => {
            pickerOpen.current = open;
          }}
        >
          <SelectTrigger
            size="sm"
            aria-label={pickerLabel}
            {...{ [SECTION_PICKER]: "" }}
            {...pickerProps}
            className="max-w-full font-medium"
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper" align="start">
            {tabs.map((tab) => (
              <SelectItem key={tab.id} value={tab.id}>
                {tab.label}
                <TabCount count={tab.count} partial={tab.partial} />
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
    </div>
  );
}
