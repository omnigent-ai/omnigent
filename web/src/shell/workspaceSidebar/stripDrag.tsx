// Dragging the strip's tabs to reorder them, as in a browser's tab strip: the tab follows
// the pointer along the strip and the others slide aside to show where it lands. Escape
// puts it back. ⌥⇧←/→ and the tab menu stay the keyboard's way to move a tab.

import {
  type AutoScrollOptions,
  closestCenter,
  type CollisionDetection,
  DndContext,
  type DragEndEvent,
  type DragStartEvent,
  type Modifier,
  MouseSensor,
  TouchSensor,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import { horizontalListSortingStrategy, SortableContext, useSortable } from "@dnd-kit/sortable";
import {
  createContext,
  type CSSProperties,
  type ReactNode,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { DRAG_MOUSE_SENSOR_OPTIONS, DRAG_TOUCH_SENSOR_OPTIONS } from "@/lib/dragSensorOptions";

import { clampAlongStrip, type StripDragBounds, stripDragBounds } from "./stripDragBounds";

/** The strip entry a tab is, while its strip can reorder; null for a ghost or a lone tab. */
export const StripEntryDragKeyContext = createContext<string | null>(null);

// Holding a tab near the strip's end scrolls the strip, and only it, sideways only: a tab
// title's line box overflows the strip a few px, which a vertical scroll would reveal.
const AUTO_SCROLL: AutoScrollOptions = {
  canScroll: (element) => element.hasAttribute("data-tab-strip-viewport"),
  threshold: { x: 0.2, y: 0 },
};
// What a screen reader hears, without the entries' internal keys. Its hidden nodes go to the
// body, so the tab list holds only tabs.
const ANNOUNCEMENTS = {
  onDragStart: () => "Picked up the tab. Release it to drop it, or press Escape to put it back.",
  onDragOver: () => undefined,
  onDragEnd: () => "Dropped the tab.",
  onDragCancel: () => "Put the tab back.",
};
// No id a strip entry can have, for a tab that isn't draggable.
const NOT_DRAGGABLE = "\u0000not-draggable";

/**
 * Lets the entries keyed `keys` be dragged; a drop calls `onReorder` with the two entries.
 * A pinned entry, keyed in `pinnedKeys`, lands only among the pinned ones, and another only
 * among the others, also where the two share one scrolling row.
 */
export function StripDragContext({
  keys,
  pinnedKeys,
  onReorder,
  children,
}: {
  keys: readonly string[];
  pinnedKeys: ReadonlySet<string>;
  onReorder: (activeKey: string, overKey: string) => void;
  children: ReactNode;
}) {
  const sensors = useSensors(
    useSensor(MouseSensor, DRAG_MOUSE_SENSOR_OPTIONS),
    useSensor(TouchSensor, DRAG_TOUCH_SENSOR_OPTIONS),
  );
  const [dragging, setDragging] = useState(false);
  useClaimEscapeWhile(dragging);
  const travel = useRef<StripDragTravel | null>(null);
  const modifiers = useMemo(() => [alongTheStrip(travel)], []);
  const collisionDetection = useMemo(() => withinItsGroup(pinnedKeys), [pinnedKeys]);
  const endDrag = () => {
    travel.current = null;
    setDragging(false);
  };
  const onDragStart = ({ activatorEvent }: DragStartEvent) => {
    travel.current = measureStripDragTravel(activatorEvent.target);
    setDragging(true);
  };
  const onDragEnd = ({ active, over }: DragEndEvent) => {
    endDrag();
    if (over && active.id !== over.id) onReorder(String(active.id), String(over.id));
  };
  return (
    <DndContext
      sensors={sensors}
      collisionDetection={collisionDetection}
      modifiers={modifiers}
      autoScroll={AUTO_SCROLL}
      accessibility={{ announcements: ANNOUNCEMENTS, container: document.body }}
      onDragStart={onDragStart}
      onDragEnd={onDragEnd}
      onDragCancel={endDrag}
    >
      <SortableContext items={[...keys]} strategy={horizontalListSortingStrategy}>
        {children}
      </SortableContext>
    </DndContext>
  );
}

/** Drops a dragged entry only among the entries in its own group: pinned, or not. */
function withinItsGroup(pinnedKeys: ReadonlySet<string>): CollisionDetection {
  return (args) => {
    const pinned = pinnedKeys.has(String(args.active.id));
    return closestCenter({
      ...args,
      droppableContainers: args.droppableContainers.filter(
        (container) => pinnedKeys.has(String(container.id)) === pinned,
      ),
    });
  };
}

/** Where a dragged tab may go, measured as the drag begins, before the tab has moved. */
interface StripDragTravel {
  viewport: HTMLElement | null;
  startScroll: number;
  bounds: StripDragBounds;
}

function measureStripDragTravel(target: EventTarget | null): StripDragTravel | null {
  const entry = target instanceof Element ? target.closest("[data-tab-strip-entry]") : null;
  // Pinned tabs and the others can share a row; a tab travels only along its own group.
  const group = entry?.hasAttribute("data-tab-pinned")
    ? "[data-tab-pinned]"
    : ":not([data-tab-pinned])";
  const entries = entry?.parentElement?.querySelectorAll(
    `:scope > [data-tab-strip-entry]${group}:not([data-tab-strip-leaving])`,
  );
  if (!entry || !entries?.length) return null;
  const viewport = entry.closest<HTMLElement>("[data-tab-strip-viewport]");
  return {
    viewport,
    startScroll: viewport?.scrollLeft ?? 0,
    bounds: stripDragBounds(
      entry.getBoundingClientRect(),
      entries[0].getBoundingClientRect(),
      entries[entries.length - 1].getBoundingClientRect(),
    ),
  };
}

/** A tab moves along the strip only, never off it, and no further than its first or last tab. */
function alongTheStrip(travel: { readonly current: StripDragTravel | null }): Modifier {
  return ({ transform }) => {
    const drag = travel.current;
    if (!drag) return { ...transform, y: 0 };
    const scrolled = (drag.viewport?.scrollLeft ?? 0) - drag.startScroll;
    return { ...transform, x: clampAlongStrip(transform.x, scrolled, drag.bounds), y: 0 };
  };
}

/**
 * While a tab is dragged, Escape only cancels the drag: it's marked handled before anything
 * else sees it, so a file tab's own Escape doesn't also close the tab.
 */
function useClaimEscapeWhile(dragging: boolean) {
  useEffect(() => {
    if (!dragging) return;
    const claim = (event: KeyboardEvent) => {
      if (event.key === "Escape") event.preventDefault();
    };
    window.addEventListener("keydown", claim, { capture: true });
    return () => window.removeEventListener("keydown", claim, { capture: true });
  }, [dragging]);
}

export interface StripEntryDrag {
  setNodeRef: (element: HTMLElement | null) => void;
  /** The pointer handlers that start a drag, for the entry's root. */
  listeners: Record<string, (event: never) => void> | undefined;
  style: CSSProperties;
  dragging: boolean;
}

/** This strip tab's drag handles, or null where it can't be dragged. */
export function useStripEntryDrag(): StripEntryDrag | null {
  const key = useContext(StripEntryDragKeyContext);
  const { setNodeRef, listeners, transform, transition, isDragging, isSorting } = useSortable({
    id: key ?? NOT_DRAGGABLE,
    disabled: key === null,
  });
  if (key === null) return null;
  // Only a drag moves tabs this way: a tab opening or closing keeps the strip's own motion.
  const moving = isSorting && transform !== null;
  return {
    setNodeRef,
    listeners: listeners as StripEntryDrag["listeners"],
    style: moving
      ? {
          transform: `translate3d(${Math.round(transform.x)}px, 0, 0)`,
          ...(transition ? { transition } : {}),
        }
      : {},
    dragging: isDragging,
  };
}
