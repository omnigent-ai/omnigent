import { useVirtualizer } from "@tanstack/react-virtual";
import {
  type FocusEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";
import { cn } from "@/lib/utils";
import { retainedRowIndices } from "@/lib/virtualRows";
import { isRowFullyVisible } from "./sidebarRowVisibility";

/** Where a windowed row sits; spread onto the row's `<li>` via its virtual props. */
export interface VirtualRowPlacement {
  index: number;
  top: number;
  measureRef: (element: HTMLElement | null) => void;
}

/**
 * Inline style that places a windowed `<li>` inside its sized list. It sets `top`, not a
 * transform, because dnd-kit ignores transforms when it measures drop targets.
 */
export function virtualRowStyle(top: number) {
  return {
    position: "absolute",
    top,
    left: 0,
    width: "100%",
  } as const;
}

// Starting guess for unmeasured rows; replaced by the first row actually measured,
// since row height follows the breakpoint and the text-size settings.
const INITIAL_ROW_ESTIMATE = 28;
const ROW_GAP = 1;
const OVERSCAN = 12;

/**
 * The element behind `ref` once it attaches. A parent's ref attaches after this
 * component's layout effects when both mount in one commit, so check again after.
 */
function useAttachedElement(ref: RefObject<HTMLElement | null>) {
  const [element, setElement] = useState<HTMLElement | null>(null);
  const sync = () => setElement(ref.current);
  useLayoutEffect(sync);
  useEffect(sync);
  return element;
}

/**
 * Offset of `listRef` from the top of the scroll content, kept current as the
 * sections above it grow, shrink, expand, or collapse.
 */
function useListScrollMargin(listRef: RefObject<HTMLElement | null>, scroller: HTMLElement | null) {
  const [margin, setMargin] = useState(0);
  const measure = useCallback(() => {
    const list = listRef.current;
    if (!list || !scroller) return;
    const next =
      list.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
    setMargin((previous) => (Math.abs(previous - next) < 0.5 ? previous : next));
  }, [listRef, scroller]);
  // Layout above the list can change on any render of the section.
  useLayoutEffect(measure);
  useLayoutEffect(() => {
    if (!scroller || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(scroller);
    for (const child of scroller.children) observer.observe(child);
    return () => observer.disconnect();
  }, [measure, scroller]);
  return margin;
}

/**
 * Render a long list of sidebar rows windowed against the sidebar's own
 * scroller, so the scrollbar spans every row and any position shows its rows
 * at once. The row last pointed at or focused stays mounted while scrolled away
 * (an open menu, a rename, a drag), and `revealId` scrolls its row into view
 * whenever it changes, the way an always-mounted row revealed itself.
 */
export function VirtualSessionRows<Item extends { id: string }>({
  items,
  scrollElementRef,
  revealId,
  revealReady = true,
  className,
  renderRow,
}: {
  items: readonly Item[];
  scrollElementRef: RefObject<HTMLElement | null>;
  /** Row to bring into view when this id changes, e.g. the active session. */
  revealId: string | null;
  /** False while rows are still arriving, so a reveal waits for the row's final position. */
  revealReady?: boolean;
  className?: string;
  renderRow: (item: Item, placement: VirtualRowPlacement) => ReactNode;
}) {
  const listRef = useRef<HTMLUListElement>(null);
  const scrollElement = useAttachedElement(scrollElementRef);
  const scrollMargin = useListScrollMargin(listRef, scrollElement);
  const indexById = useMemo(() => new Map(items.map((item, index) => [item.id, index])), [items]);
  const [interactedId, setInteractedId] = useState<string | null>(null);
  // Kept apart from the last pointed-at row: a touch scroll presses other rows while a rename
  // field keeps focus, and that row must stay mounted until focus leaves it.
  const [focusedId, setFocusedId] = useState<string | null>(null);
  const interactedIndex = interactedId === null ? -1 : (indexById.get(interactedId) ?? -1);
  const focusedIndex = focusedId === null ? -1 : (indexById.get(focusedId) ?? -1);
  const rangeExtractor = useCallback(
    (range: Parameters<typeof retainedRowIndices>[0]) =>
      retainedRowIndices(range, [interactedIndex, focusedIndex]),
    [interactedIndex, focusedIndex],
  );
  // Stable per list: the virtualizer rebuilds every row's measurement when this changes.
  const getItemKey = useCallback((index: number) => items[index]?.id ?? index, [items]);
  const [rowEstimate, setRowEstimate] = useState(INITIAL_ROW_ESTIMATE);
  const estimateMeasured = useRef(false);
  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => scrollElement,
    estimateSize: () => rowEstimate,
    gap: ROW_GAP,
    overscan: OVERSCAN,
    scrollMargin,
    getItemKey,
    rangeExtractor,
  });

  // Size unmeasured rows like the first real one, so the scrollbar doesn't drift.
  const { measureElement } = virtualizer;
  const measureRow = useCallback(
    (element: HTMLElement | null) => {
      measureElement(element);
      if (!element || estimateMeasured.current) return;
      const height = element.getBoundingClientRect().height;
      if (height <= 0) return;
      estimateMeasured.current = true;
      setRowEstimate(height);
    },
    [measureElement],
  );
  useLayoutEffect(() => {
    virtualizer.measure();
  }, [rowEstimate, virtualizer]);

  // Only this list's own rows count; another list or the transcript reuses data-index.
  const rowIdOf = useCallback(
    (target: EventTarget | null): string | undefined => {
      const list = listRef.current;
      let row = target instanceof Element ? target : null;
      while (row && row.parentElement !== list) row = row.parentElement;
      const index = row instanceof HTMLElement ? Number(row.dataset.index) : Number.NaN;
      return items[index]?.id;
    },
    [items],
  );
  // Remember the row a pointer or focus last entered, so it survives scrolling.
  const trackInteraction = useCallback(
    (target: EventTarget | null) => {
      const id = rowIdOf(target);
      if (id !== undefined) setInteractedId(id);
    },
    [rowIdOf],
  );
  const trackFocus = useCallback(
    (target: EventTarget | null) => {
      trackInteraction(target);
      const id = rowIdOf(target);
      if (id !== undefined) setFocusedId(id);
    },
    [rowIdOf, trackInteraction],
  );
  // Focus moving out of a row, to another row or out of the list, releases it.
  const releaseFocus = useCallback(
    (event: FocusEvent<HTMLUListElement>) => {
      const from = rowIdOf(event.target);
      if (from !== undefined && rowIdOf(event.relatedTarget) !== from) {
        setFocusedId((current) => (current === from ? null : current));
      }
    },
    [rowIdOf],
  );

  // Reveal once per change of `revealId`, once its row and the scroller both exist.
  const pendingReveal = useRef<string | null>(null);
  const lastRevealId = useRef<string | null>(null);
  if (revealId !== lastRevealId.current) {
    lastRevealId.current = revealId;
    pendingReveal.current = revealId;
  }
  useEffect(() => {
    const id = pendingReveal.current;
    if (id === null || !revealReady || !scrollElement) return;
    const index = indexById.get(id);
    if (index === undefined) return;
    pendingReveal.current = null;
    const mounted = listRef.current?.querySelector(`:scope > [data-index="${index}"]`);
    if (mounted instanceof HTMLElement) {
      // A row already fully on screen stays put, so the list doesn't slide.
      if (!isRowFullyVisible(mounted))
        mounted.scrollIntoView({ behavior: "smooth", block: "center" });
    } else virtualizer.scrollToIndex(index, { align: "center" });
  }, [revealId, revealReady, indexById, virtualizer, scrollElement]);

  return (
    <ul
      ref={listRef}
      className={cn("relative", className)}
      style={{ height: virtualizer.getTotalSize() }}
      onPointerDownCapture={(event) => trackInteraction(event.target)}
      onFocusCapture={(event) => trackFocus(event.target)}
      onBlurCapture={releaseFocus}
    >
      {virtualizer.getVirtualItems().map((virtualRow) => {
        const item = items[virtualRow.index];
        if (!item) return null;
        return renderRow(item, {
          index: virtualRow.index,
          top: virtualRow.start - scrollMargin,
          measureRef: measureRow,
        });
      })}
    </ul>
  );
}
