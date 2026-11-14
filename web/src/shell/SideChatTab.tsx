// Side-chat tabs in the Workspace strip and the mobile Side chats drawer:
// density by tab count, the right-click menu, and drag-to-reorder.

import {
  closestCenter,
  type CollisionDetection,
  DndContext,
  type DragEndEvent,
  MouseSensor,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import { horizontalListSortingStrategy, SortableContext, useSortable } from "@dnd-kit/sortable";
import { ArrowLeftIcon, ArrowRightIcon, PinIcon, PinOffIcon, XIcon } from "lucide-react";
import type { ComponentType, ReactNode, Ref } from "react";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import { cn } from "@/lib/utils";

/** How tightly side-chat tabs pack: they shrink as more of them are open. */
export type SideChatTabDensity = "regular" | "compact" | "dense";

const COMPACT_FROM_TABS = 4;
const DENSE_FROM_TABS = 7;

/** Choose density from the combined side-chat and sub-agent tab count. */
export function sideChatTabDensity(tabCount: number): SideChatTabDensity {
  if (tabCount >= DENSE_FROM_TABS) return "dense";
  if (tabCount >= COMPACT_FROM_TABS) return "compact";
  return "regular";
}

/** Dense strips show Close only on the selected tab; the menu still offers it. */
export function showsSideChatCloseButton(density: SideChatTabDensity, active: boolean): boolean {
  return density !== "dense" || active;
}

/** The pin and move actions a side-chat tab offers; absent for sub-agent tabs. */
export interface SideChatTabArrangement {
  pinned: boolean;
  canMoveLeft: boolean;
  canMoveRight: boolean;
  onPinnedChange: (pinned: boolean) => void;
  onMoveLeft: () => void;
  onMoveRight: () => void;
}

/** Right-click (or long-press) menu for one side-chat tab. */
export function SideChatTabMenu({
  label,
  arrangement,
  onClose,
  children,
}: {
  label: string;
  arrangement: SideChatTabArrangement | null;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>{children}</ContextMenuTrigger>
      <ContextMenuContent aria-label={`${label} actions`} className="min-w-40">
        {arrangement && (
          <>
            <ContextMenuItem onSelect={() => arrangement.onPinnedChange(!arrangement.pinned)}>
              {arrangement.pinned ? <PinOffIcon /> : <PinIcon />}
              {arrangement.pinned ? "Unpin tab" : "Pin tab"}
            </ContextMenuItem>
            <ContextMenuItem disabled={!arrangement.canMoveLeft} onSelect={arrangement.onMoveLeft}>
              <ArrowLeftIcon />
              Move left
            </ContextMenuItem>
            <ContextMenuItem
              disabled={!arrangement.canMoveRight}
              onSelect={arrangement.onMoveRight}
            >
              <ArrowRightIcon />
              Move right
            </ContextMenuItem>
            <ContextMenuSeparator />
          </>
        )}
        <ContextMenuItem onSelect={onClose}>
          <XIcon />
          Close tab
        </ContextMenuItem>
      </ContextMenuContent>
    </ContextMenu>
  );
}

const TAB_ACTIVE =
  "bg-[color-mix(in_srgb,var(--muted-foreground)_15%,var(--card))] text-foreground";
const TAB_INACTIVE =
  "text-muted-foreground hover:bg-[color-mix(in_srgb,var(--muted-foreground)_15%,var(--card))] hover:text-foreground";

/** One side-chat or sub-agent tab in the Workspace strip; draggable when `arrangement` is set. */
export function SideChatRailTab({
  id,
  label,
  compactLabel,
  icon: Icon,
  active,
  density,
  arrangement,
  tabRef,
  onSelect,
  onClose,
}: {
  id: string;
  /** Full name, always the tab's accessible name and tooltip. */
  label: string;
  /** Shorter visible text once the strip is compact (a side chat's number). */
  compactLabel: string;
  icon: ComponentType<{ className?: string }>;
  active: boolean;
  density: SideChatTabDensity;
  arrangement: SideChatTabArrangement | null;
  tabRef?: Ref<HTMLDivElement>;
  onSelect: () => void;
  onClose: () => void;
}) {
  const sortable = useSortable({ id, disabled: arrangement === null });
  const pinned = arrangement?.pinned ?? false;
  const regular = density === "regular";
  // Only the horizontal offset: the strip is one row, so a drag never moves a tab vertically.
  const transform = sortable.transform
    ? `translate3d(${Math.round(sortable.transform.x)}px, 0, 0)`
    : undefined;
  const setRefs = (node: HTMLDivElement | null) => {
    sortable.setNodeRef(node);
    if (typeof tabRef === "function") tabRef(node);
    else if (tabRef) tabRef.current = node;
  };
  return (
    <SideChatTabMenu label={label} arrangement={arrangement} onClose={onClose}>
      <div
        ref={setRefs}
        data-side-chat-tab={id}
        data-density={density}
        data-pinned={pinned || undefined}
        style={{ transform, transition: sortable.transition }}
        {...sortable.listeners}
        className={cn(
          "flex h-[24px] shrink-0 items-center rounded-md text-ui font-medium leading-5 transition-colors",
          regular ? "gap-[6px] px-2" : "gap-1 px-1.5",
          active ? TAB_ACTIVE : TAB_INACTIVE,
          sortable.isDragging && "relative z-10 opacity-80",
        )}
      >
        <button
          type="button"
          role="tab"
          aria-selected={active}
          aria-label={label}
          title={label}
          className="flex min-w-0 items-center gap-1"
          onAuxClick={(event) => {
            if (event.button === 1) {
              event.preventDefault();
              onClose();
            }
          }}
          onClick={onSelect}
        >
          <Icon className="size-4 shrink-0" />
          <span className={cn("truncate", regular ? "max-w-[16ch]" : "max-w-[8ch]")}>
            {regular ? label : compactLabel}
          </span>
        </button>
        {pinned ? (
          <button
            type="button"
            aria-label={`Unpin ${label}`}
            title="Unpin"
            className="flex size-4 items-center justify-center rounded hover:bg-muted"
            onClick={() => arrangement?.onPinnedChange(false)}
          >
            <PinIcon className="size-3" />
          </button>
        ) : (
          showsSideChatCloseButton(density, active) && (
            <button
              type="button"
              aria-label={`Close ${label}`}
              className="flex size-4 items-center justify-center rounded hover:bg-muted"
              onClick={onClose}
            >
              <XIcon className="size-3" />
            </button>
          )
        )}
      </div>
    </SideChatTabMenu>
  );
}

// A small drag threshold keeps a plain click selecting the tab. Mouse only: on touch,
// a swipe scrolls the strip and a long press opens the menu, which keyboard and touch
// users reorder from, so Space and Enter still activate the tab.
const MOUSE_SENSOR_OPTIONS = { activationConstraint: { distance: 5 } };

/** Closest tab in the dragged tab's own pinned or unpinned group, so the other group never shifts. */
export const closestInSameGroup: CollisionDetection = (args) => {
  const group = args.active.data.current?.sortable?.containerId;
  return closestCenter({
    ...args,
    droppableContainers: args.droppableContainers.filter(
      (container) => container.data.current?.sortable?.containerId === group,
    ),
  });
};

let announcementHost: HTMLElement | null = null;

/**
 * dnd-kit's screen-reader text goes before the app: after it, double-click stops
 * selecting words in the Markdown editor; inside the Workspace it adds a second status.
 */
function dragAnnouncementHost(): HTMLElement {
  if (!announcementHost?.isConnected) {
    announcementHost = document.createElement("div");
    document.body.prepend(announcementHost);
  }
  return announcementHost;
}

/** Drag context for the strip's side-chat tabs; `onMove` gets the dragged and target ids. */
export function SideChatTabsDragArea({
  onMove,
  children,
}: {
  onMove: (childId: string, targetId: string) => void;
  children: ReactNode;
}) {
  const sensors = useSensors(useSensor(MouseSensor, MOUSE_SENSOR_OPTIONS));
  const onDragEnd = ({ active, over }: DragEndEvent) => {
    if (over && active.id !== over.id) onMove(String(active.id), String(over.id));
  };
  return (
    <DndContext
      sensors={sensors}
      collisionDetection={closestInSameGroup}
      onDragEnd={onDragEnd}
      accessibility={{ container: dragAnnouncementHost() }}
    >
      {children}
    </DndContext>
  );
}

/** One group of side-chat tabs that reorder among themselves. */
export function SideChatTabGroup({ ids, children }: { ids: string[]; children: ReactNode }) {
  return (
    <SortableContext items={ids} strategy={horizontalListSortingStrategy}>
      {children}
    </SortableContext>
  );
}
