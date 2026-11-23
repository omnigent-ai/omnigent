// A workspace page whose toolbar spans its full width, with an optional sidebar
// (a file tree) below the toolbar and beside the content. Showing or hiding the
// sidebar never moves or narrows the toolbar. A narrow page opens the sidebar
// as a popover under its toggle instead, as the Changes review does.

import { ListTreeIcon } from "lucide-react";
import { useCallback, useRef, useState, type ReactNode } from "react";

import { WIDE_REVIEW_MIN_WIDTH, useElementWidth } from "@/components/diff-review/reviewLayout";
import { PaneResizeHandle } from "@/components/PaneResizeHandle";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import {
  useRememberedPaneWidth,
  type PaneWidthSpec,
  type RememberedPaneWidth,
} from "@/hooks/useRememberedPaneWidth";
import { readSidebarVisible, writeSidebarVisible } from "@/lib/sidebarVisibility";
import { cn } from "@/lib/utils";

/**
 * Grid placement for a page inside ``WorkspaceSidebarLayout``. The page spans
 * the grid and shares its tracks, so a component that keeps its toolbar and
 * content in one root (the file viewer) can still let the sidebar sit between them.
 */
export const WORKSPACE_SIDEBAR_LAYOUT = {
  page: "col-span-full row-span-full grid min-h-0 min-w-0 grid-cols-subgrid grid-rows-subgrid overflow-hidden",
  toolbar: "col-span-full row-start-1",
  content: "col-start-2 row-start-2 min-w-0",
} as const;

/** The compact toolbar row workspace pages share, matching the Changes review. */
export const WORKSPACE_TOOLBAR_CLASS =
  "flex min-h-9 shrink-0 items-center gap-1 border-b border-border px-2 py-0.5 text-ui";

export interface WorkspaceSidebarState {
  /** Wide enough to dock the sidebar; narrower pages open it as a popover. */
  wide: boolean;
  /** A narrow page with nothing beside the sidebar shows it across the page instead of a popover. */
  fills: boolean;
  /** Whether the sidebar is on screen, docked or as a popover. */
  shown: boolean;
  toggle: () => void;
  /** Puts the sidebar on screen: docks it on a wide page, opens the popover on a narrow one. */
  show: () => void;
  /** Closes the popover; a docked sidebar stays. */
  dismissPopover: () => void;
  setPopoverOpen: (open: boolean) => void;
  popoverOpen: boolean;
  setElement: (element: HTMLElement | null) => void;
  /** The page, once mounted, and its measured width (null before it's measured). */
  element: HTMLElement | null;
  pageWidth: number | null;
}

/** A docked sidebar's chosen width and its resize handle, from ``useWorkspaceSidebarWidth``. */
export type WorkspaceSidebarResize = RememberedPaneWidth;

/**
 * A page's sidebar visibility; the docked choice is remembered per device under
 * ``storageKey``. With ``fillWhenNarrow`` (nothing to show beside it), a narrow
 * page shows the sidebar across its width rather than as a popover.
 */
export function useWorkspaceSidebar(
  storageKey: string,
  { fillWhenNarrow = false }: { fillWhenNarrow?: boolean } = {},
): WorkspaceSidebarState {
  const [element, setElement] = useState<HTMLElement | null>(null);
  const width = useElementWidth(element);
  // An unmeasured page (first paint, tests) counts as wide.
  const measuredWide = width === null || width === 0 || width >= WIDE_REVIEW_MIN_WIDTH;
  const fills = !measuredWide && fillWhenNarrow;
  // A filled page docks its sidebar, so it shows and toggles as a wide one does.
  const wide = measuredWide || fills;
  const [docked, setDocked] = useState(() => readSidebarVisible(storageKey, true));
  const [popoverOpen, setPopoverOpen] = useState(false);
  const toggle = useCallback(() => {
    if (!wide) {
      setPopoverOpen((open) => !open);
      return;
    }
    setDocked((open) => {
      writeSidebarVisible(storageKey, !open);
      return !open;
    });
  }, [storageKey, wide]);
  const show = useCallback(() => {
    if (!wide) {
      setPopoverOpen(true);
      return;
    }
    writeSidebarVisible(storageKey, true);
    setDocked(true);
  }, [storageKey, wide]);
  const dismissPopover = useCallback(() => setPopoverOpen(false), []);
  return {
    wide,
    fills,
    shown: wide ? docked : popoverOpen,
    toggle,
    show,
    dismissPopover,
    popoverOpen: !wide && popoverOpen,
    setPopoverOpen,
    setElement,
    element,
    pageWidth: width,
  };
}

/** Lets a docked sidebar be resized from its edge, within `spec`'s limits in this page. */
export function useWorkspaceSidebarWidth(
  state: WorkspaceSidebarState,
  spec: PaneWidthSpec,
): WorkspaceSidebarResize {
  const page = useRef<HTMLElement | null>(null);
  page.current = state.element;
  // A popover or a sidebar across the page isn't beside anything, so its page doesn't size it.
  const docked = state.wide && state.shown && !state.fills;
  return useRememberedPaneWidth(spec, page, docked ? state.pageWidth || null : null);
}

export function WorkspaceSidebarLayout({
  state,
  resize,
  sidebar,
  sidebarLabel,
  children,
  "data-testid": testId,
}: {
  /** From ``useWorkspaceSidebar``; omit it for a page without a sidebar. */
  state?: WorkspaceSidebarState;
  /** From ``useWorkspaceSidebarWidth``; without it a docked sidebar has a fixed width. */
  resize?: WorkspaceSidebarResize;
  sidebar?: ReactNode;
  /** Names the sidebar's popover for assistive technology. */
  sidebarLabel: string;
  /** The page, placed with ``WORKSPACE_SIDEBAR_LAYOUT``. */
  children: ReactNode;
  "data-testid"?: string;
}) {
  const docked = state?.wide === true && state.shown && sidebar;
  return (
    <Popover open={state?.popoverOpen ?? false} onOpenChange={state?.setPopoverOpen}>
      <div
        ref={state?.setElement}
        data-testid={testId}
        className="@container/workspace-page grid min-h-0 flex-1 grid-cols-[auto_minmax(0,1fr)] grid-rows-[auto_minmax(0,1fr)]"
      >
        {children}
        {/* After the page, so it paints over the page's empty first column. */}
        {docked && (
          <div
            className={cn(
              "row-start-2 flex min-h-0 flex-col",
              state.fills ? "col-span-full" : "relative col-start-1 border-r border-border",
              !state.fills && !resize && "w-[min(16rem,40cqw)]",
            )}
            style={!state.fills && resize ? { width: resize.width } : undefined}
          >
            {sidebar}
            {!state.fills && resize && (
              <PaneResizeHandle resize={resize} testId="workspace-sidebar-resize-handle" />
            )}
          </div>
        )}
      </div>
      {state && !state.wide && sidebar && (
        <PopoverContent
          align="start"
          collisionPadding={8}
          className="flex h-96 w-72 flex-col overflow-hidden p-0"
          aria-label={sidebarLabel}
          // A sidebar marks the control to start on with data-sidebar-initial-focus, such as
          // the tree's Go to File; without one the popover starts on its first control.
          onOpenAutoFocus={(event) => {
            const target = (event.currentTarget as HTMLElement).querySelector<HTMLElement>(
              "[data-sidebar-initial-focus]",
            );
            target?.focus();
            // One that can't take focus leaves the popover's first control.
            if (target && document.activeElement === target) event.preventDefault();
          }}
        >
          {sidebar}
        </PopoverContent>
      )}
    </Popover>
  );
}

/**
 * The toolbar button that shows or hides the sidebar. On a narrow page it is the
 * popover's trigger, so the popover anchors to it and returns focus to it on close.
 */
export function WorkspaceSidebarToggle({
  state,
  showLabel,
  hideLabel,
}: {
  state: WorkspaceSidebarState;
  showLabel: string;
  hideLabel: string;
}) {
  const label = state.shown ? hideLabel : showLabel;
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <PopoverTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label={label}
              // A docked sidebar is a pressed toggle, not a popup.
              aria-pressed={state.wide ? state.shown : undefined}
              aria-haspopup={state.wide ? undefined : "dialog"}
              aria-expanded={state.wide ? undefined : state.popoverOpen}
              data-state={state.shown ? "on" : "off"}
              className="shrink-0 text-muted-foreground data-[state=on]:bg-accent data-[state=on]:text-accent-foreground"
              onClick={(event) => {
                // One toggle for docking and the popover alike, as the page's command runs;
                // skip the trigger's own, which would toggle the popover a second time.
                event.preventDefault();
                state.toggle();
              }}
            >
              <ListTreeIcon />
            </Button>
          </PopoverTrigger>
        </TooltipTrigger>
        <TooltipContent>{label}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
