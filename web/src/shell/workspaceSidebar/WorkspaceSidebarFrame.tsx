// The body of a workspace sidebar, whoever hosts it: one row of tabs with the host's
// controls around it, an optional banner, and the tabs' content, where a tab the user
// visited keeps its state while another shows. The host owns the sidebar's root, so it
// decides where the sidebar sits, its width and its command region.

import {
  type CSSProperties,
  type ReactElement,
  type ReactNode,
  type RefObject,
  useRef,
} from "react";

import { Tabs, TabsList } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";

import { TabStripScroller } from "../TabStripScroller";
import type { TabStripWidth } from "../workspaceTabs/useTabStripWidth";
import type { TabId } from "../workspaceTabs/workspacePlace";
import { TAB_GAP } from "../workspaceTabs/tabStripWidth";
import { StripDragContext, StripEntryDragKeyContext } from "./stripDrag";
import { TAB_STRIP_MOTION, tabStripMotionStyle } from "./tabStripMotion";
import { StripEntryMotionContext, useTabStripPresence } from "./useTabStripPresence";

/** The strip's motion timings and its tab gap, for the CSS that moves its tabs. */
const STRIP_MOTION_STYLE = {
  ...tabStripMotionStyle(TAB_STRIP_MOTION),
  "--tab-gap": `${TAB_GAP}px`,
} as CSSProperties;

/** One entry of the strip: a `StripTab`, and the key that follows it as it comes and goes. */
export interface WorkspaceSidebarStripEntry {
  key: string;
  tab: ReactElement;
  /** Kept at the strip's start, in the pinned group that stays put as the strip scrolls. */
  pinned?: boolean;
}

export interface WorkspaceSidebarStripTabs {
  /** The selected entry's ID, or "" for none. */
  value: string;
  onValueChange: (tabId: TabId) => void;
  /** Names the strip in usage events. */
  componentId: string;
  /** Whose tabs these are, such as the session's: another scope's tabs show without motion. */
  scope: string;
  /** The strip's entries, in order. */
  entries: readonly WorkspaceSidebarStripEntry[];
  /** Lets a tab be dragged to `overKey`'s place; without it, the strip's tabs stay put. */
  onReorder?: (activeKey: string, overKey: string) => void;
  /**
   * The pinned tabs stay put beside the others as they scroll; otherwise, such as where they
   * don't all fit, they scroll first among the others.
   */
  pinsStayPut?: boolean;
}

export function WorkspaceSidebarFrame({
  stripWidth,
  tabListRef,
  leading,
  tabs,
  placeholder,
  afterTabs,
  trailing,
  banner,
  contentRef,
  children,
}: {
  stripWidth: TabStripWidth;
  tabListRef?: RefObject<HTMLDivElement | null>;
  /** Before the tabs, such as Back and Forward. */
  leading?: ReactNode;
  /** Null shows `placeholder` in the tabs' place, such as while the workspace starts. */
  tabs: WorkspaceSidebarStripTabs | null;
  placeholder?: ReactNode;
  /** Right after the last tab, such as "+". */
  afterTabs?: ReactNode;
  /** At the strip's far end, such as the tab list or a close button. */
  trailing?: ReactNode;
  /** Between the strip and the content, such as a notice about the selected tab. */
  banner?: ReactNode;
  contentRef?: RefObject<HTMLDivElement | null>;
  /** The tabs' content: a `WorkspaceSidebarTabPanel` each, and anything shown over them. */
  children: ReactNode;
}) {
  return (
    <>
      <div
        ref={tabListRef}
        role="toolbar"
        aria-label="Workspace tabs"
        tabIndex={-1}
        {...stripWidth.pointerHandlers}
        className="workspace-tab-strip flex shrink-0 items-center gap-1 overflow-x-hidden border-b border-border px-2 py-3"
      >
        {leading}
        {/* The tabs and "+" share this room; "+" follows the last tab until the strip overflows. */}
        <div ref={stripWidth.areaRef} className="flex min-w-0 flex-1 items-center gap-1">
          {tabs === null ? (
            placeholder
          ) : (
            <Tabs
              value={tabs.value}
              onValueChange={tabs.onValueChange}
              activationMode="manual"
              className="min-w-0"
              componentId={tabs.componentId}
            >
              {/* One tab list across the pinned tabs and the others, so focus walks them all. */}
              <TabsList
                variant="pill"
                className="flex min-w-0 max-w-full justify-start gap-0.5"
                aria-label="Workspace tabs"
                data-tab-reflow={stripWidth.reflowing || undefined}
                style={STRIP_MOTION_STYLE}
              >
                <StripEntries
                  scope={tabs.scope}
                  entries={tabs.entries}
                  onReorder={tabs.onReorder}
                  stripWidth={stripWidth}
                  pinsStayPut={tabs.pinsStayPut ?? true}
                />
              </TabsList>
            </Tabs>
          )}
          {afterTabs}
        </div>
        <div className="ml-auto flex shrink-0 items-center gap-1 pl-1">{trailing}</div>
      </div>
      {banner}
      <div
        ref={contentRef}
        data-workspace-panel-content
        className="relative flex min-h-0 flex-1 flex-col overflow-hidden"
      >
        {children}
      </div>
    </>
  );
}

/** The strip's tabs, with closed ones briefly kept as ghosts that shrink away. */
function StripEntries({
  scope,
  entries,
  onReorder,
  stripWidth,
  pinsStayPut,
}: {
  scope: string;
  entries: readonly WorkspaceSidebarStripEntry[];
  onReorder?: (activeKey: string, overKey: string) => void;
  stripWidth: TabStripWidth;
  pinsStayPut: boolean;
}) {
  const present = useTabStripPresence(scope, entries);
  // A closing tab's ghost shrinks away in the group the tab was in.
  const pinnedKeys = useRef(new Map<string, boolean>());
  for (const entry of entries) pinnedKeys.current.set(entry.key, entry.pinned === true);
  const presentKeys = new Set(present.map((entry) => entry.key));
  for (const key of pinnedKeys.current.keys()) {
    if (!presentKeys.has(key)) pinnedKeys.current.delete(key);
  }
  const render = ({ key, motion, tab }: (typeof present)[number]) => (
    <StripEntryMotionContext.Provider key={key} value={motion}>
      {/* A closing tab's ghost can't be dragged. */}
      <StripEntryDragKeyContext.Provider
        value={onReorder && motion.phase !== "leaving" ? key : null}
      >
        {tab}
      </StripEntryDragKeyContext.Provider>
    </StripEntryMotionContext.Provider>
  );
  const pinned = pinsStayPut ? present.filter((entry) => pinnedKeys.current.get(entry.key)) : [];
  const unpinned = pinsStayPut
    ? present.filter((entry) => !pinnedKeys.current.get(entry.key))
    : present;
  const shown = (
    <>
      {pinned.length > 0 && (
        // Pinned tabs stay put beside the others as they scroll.
        <TabStripScroller
          data-tab-strip-pinned
          className="no-drag shrink-0 border-r border-border pr-0.5"
          contentClassName="workspace-tab-list gap-0.5"
          fadeEdges
        >
          {pinned.map(render)}
        </TabStripScroller>
      )}
      <TabStripScroller
        data-workspace-tabs-viewport
        className="no-drag min-w-0"
        contentClassName="workspace-tab-list gap-0.5"
        fadeEdges
        holdExtent={stripWidth.holding}
        contentRef={stripWidth.rowRef}
      >
        {unpinned.map(render)}
      </TabStripScroller>
    </>
  );
  if (!onReorder) return shown;
  return (
    <StripDragContext
      keys={entries.map((entry) => entry.key)}
      pinnedKeys={new Set(entries.filter((entry) => entry.pinned).map((entry) => entry.key))}
      onReorder={onReorder}
    >
      {shown}
    </StripDragContext>
  );
}

/** One tab's content. A hidden one keeps its layout and scroll, ready to show again. */
export function WorkspaceSidebarTabPanel({
  tabId,
  selected,
  children,
}: {
  tabId: TabId;
  selected: boolean;
  children: ReactNode;
}) {
  return (
    <div
      data-workspace-tab-panel={tabId}
      className={cn(
        "flex min-h-0 flex-1 flex-col",
        // Not display:none: hidden rows would measure 0px and collapse
        // a virtualized tree's height, losing its scroll position.
        !selected && "invisible absolute inset-0",
      )}
    >
      {children}
    </div>
  );
}
