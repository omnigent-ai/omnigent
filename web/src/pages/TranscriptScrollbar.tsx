// The transcript's scrollbar.
//
// The native bar is hidden (the transcript is lazily paginated and the native
// thumb can't be styled or dragged the way this one can), so this one stands
// in for it: the thumb's length is the visible share of the loaded document,
// down to a minimum grab size, and its travel is the rest of the track. A
// transcript that is one screen and a few lines shows a thumb nearly the
// length of the track that moves a few pixels for a few pixels of scrolling; a
// long one shows a short thumb. A fixed-length thumb instead maps whatever
// range exists onto the whole track, so a 30px range sent it flying end to end
// on a single wheel tick.

import { useCallback, useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";

/** The scroll container plus the bottom-lock release, as lifted by ChatPage. */
interface Scroller {
  el: HTMLElement;
  stopScroll: () => void;
}

/** Smallest thumb: enough to grab, however long the document. */
const MIN_THUMB_PX = 56;
/** Smallest travel a scrollable transcript keeps, so the thumb can always be dragged. */
const MIN_TRAVEL_PX = 8;

/**
 * Dispatched on the scroll container for each thumb-drag move, with the
 * direction the reader is dragging the transcript. The history loader listens
 * for it: dragging up asks for older history, dragging back down withdraws.
 */
export const TRANSCRIPT_SCROLLBAR_DRAG_EVENT = "transcript-scrollbar-drag";
export interface TranscriptScrollbarDragDetail {
  direction: "up" | "down";
}
/**
 * Smallest scroll range worth a thumb. Fractional content heights round into
 * `scrollHeight`, and LatestTurnSpacer's 1px write-hysteresis can leave the
 * document eight pixels taller than the viewport — painting a thumb for that
 * noise advertises hidden content that does not exist. The trade: a real
 * range under 9px also goes unindicated — imperceptible at these sizes.
 */
const MIN_SCROLL_RANGE_PX = 9;
/** Track inset from the top, clearing the ChatHeader overlay's controls. */
const TRACK_TOP_PX = 64;
const TRACK_BOTTOM_PX = 12;

interface ThumbDrag {
  /** The pointer's position at the previous move. */
  lastY: number;
  /**
   * How far the pointer is past the point where it holds the thumb, in track
   * pixels: travel the transcript couldn't follow at its top or bottom. A move
   * back uses it up first, so the thumb waits for the pointer to return.
   */
  overshootPx: number;
  /** Where this drag left the thumb, to notice when something else moved it. */
  thumbOffsetPx: number;
}

/**
 * Keep the pointer's overshoot measured from the thumb after something other
 * than the drag moved it, such as the transcript holding the reader while a
 * history page lands. Once the thumb has caught up with the pointer, nothing
 * is owed.
 */
export function reanchorOvershoot(overshootPx: number, thumbMovedPx: number): number {
  const reanchored = overshootPx - thumbMovedPx;
  return Math.sign(reanchored) === Math.sign(overshootPx) ? reanchored : 0;
}

/**
 * Split a pointer move into the part that pays back overshoot and the part
 * that scrolls. Only a move back toward the thumb pays it back; a move further
 * away scrolls by its own length, so a held transcript never jumps.
 */
export function splitThumbMove(
  overshootPx: number,
  movedPx: number,
): { overshootPx: number; scrollPx: number } {
  if (overshootPx === 0 || Math.sign(movedPx) === Math.sign(overshootPx)) {
    return { overshootPx, scrollPx: movedPx };
  }
  const repaid = Math.min(Math.abs(movedPx), Math.abs(overshootPx)) * Math.sign(movedPx);
  return { overshootPx: overshootPx + repaid, scrollPx: movedPx - repaid };
}

export function TranscriptScrollbar({
  scroller,
  // Top inset of the track. Defaults to clearing the floating ChatHeader; when
  // the Plan accordion is pinned above, the scroll container already starts
  // below the header, so the caller passes a small inset instead — otherwise
  // the thumb can't reach the top of the (already-cleared) viewport.
  topInset = TRACK_TOP_PX,
}: {
  scroller: Scroller | null;
  topInset?: number;
}) {
  const [offset, setOffset] = useState(0);
  const [thumbPx, setThumbPx] = useState(MIN_THUMB_PX);
  const [scrollable, setScrollable] = useState(false);
  const [dragging, setDragging] = useState(false);
  // Each move scrolls by the distance since the previous one rather than from
  // where the thumb was grabbed: a history page can land mid-drag, and the
  // transcript's hold for it must survive the next move.
  const dragRef = useRef<ThumbDrag | null>(null);

  const el = scroller?.el ?? null;

  /** Thumb length and travel for the document as loaded: the visible share of the track, then the rest. */
  const geometryOf = useCallback(
    (node: HTMLElement) => {
      const track = node.clientHeight - topInset - TRACK_BOTTOM_PX;
      const thumb = Math.min(
        track - MIN_TRAVEL_PX,
        Math.max(MIN_THUMB_PX, Math.round((track * node.clientHeight) / node.scrollHeight)),
      );
      return { thumb, travel: track - thumb };
    },
    [topInset],
  );

  /** The thumb's distance down its track, unrounded. */
  const thumbOffsetOf = useCallback(
    (node: HTMLElement) => {
      const max = node.scrollHeight - node.clientHeight;
      if (max <= 0) return 0;
      return Math.min(1, Math.max(0, node.scrollTop / max)) * geometryOf(node).travel;
    },
    [geometryOf],
  );

  useEffect(() => {
    // A new scroller means a new thumb; any drag belonged to the old one.
    dragRef.current = null;
    setDragging(false);
    if (!el) return;
    const measure = () => {
      const max = el.scrollHeight - el.clientHeight;
      const { thumb, travel } = geometryOf(el);
      // A pane too short for a grab-sized thumb plus travel draws no bar; a
      // drag in progress ends with it, since the thumb it held is gone.
      if (max < MIN_SCROLL_RANGE_PX || travel <= 0 || thumb < MIN_THUMB_PX) {
        dragRef.current = null;
        setDragging(false);
        setScrollable(false);
        return;
      }
      setScrollable(true);
      setThumbPx(thumb);
      setOffset(Math.round(thumbOffsetOf(el)));
    };
    measure();
    el.addEventListener("scroll", measure, { passive: true });
    // Content grows on stream and on every history page; the container itself
    // resizes when the workspace panel or the soft keyboard opens.
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    if (el.firstElementChild) observer.observe(el.firstElementChild);
    return () => {
      el.removeEventListener("scroll", measure);
      observer.disconnect();
    };
  }, [el, geometryOf, thumbOffsetOf]);

  const onPointerDown = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      // Only the primary button drags; a right-click keeps its context menu.
      if (!el || !scroller || event.button !== 0) return;
      event.preventDefault();
      event.currentTarget.setPointerCapture(event.pointerId);
      // StickToBottom re-pins to the bottom on content resize unless the lock
      // is released, which would fight a drag away from the newest turn.
      scroller.stopScroll();
      dragRef.current = { lastY: event.clientY, overshootPx: 0, thumbOffsetPx: thumbOffsetOf(el) };
      setDragging(true);
    },
    [el, scroller, thumbOffsetOf],
  );

  const onPointerMove = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      const drag = dragRef.current;
      if (!drag || !el) return;
      const { travel } = geometryOf(el);
      if (travel <= 0) return;
      const max = el.scrollHeight - el.clientHeight;
      const moved = event.clientY - drag.lastY;
      drag.lastY = event.clientY;
      const owed = reanchorOvershoot(drag.overshootPx, thumbOffsetOf(el) - drag.thumbOffsetPx);
      const { overshootPx, scrollPx } = splitThumbMove(owed, moved);
      const scrollPerPx = max / travel;
      const before = el.scrollTop;
      el.scrollTop = before + scrollPx * scrollPerPx;
      // Travel the transcript refused at its top or bottom stays owed to the
      // pointer; sub-pixel rounding elsewhere is not overshoot.
      const atEnd = scrollPx < 0 ? el.scrollTop <= 0 : el.scrollTop >= max - 1;
      const refusedPx = atEnd ? scrollPx - (el.scrollTop - before) / scrollPerPx : 0;
      drag.overshootPx = overshootPx + refusedPx;
      drag.thumbOffsetPx = thumbOffsetOf(el);
      if (moved !== 0) {
        el.dispatchEvent(
          new CustomEvent<TranscriptScrollbarDragDetail>(TRANSCRIPT_SCROLLBAR_DRAG_EVENT, {
            detail: { direction: moved < 0 ? "up" : "down" },
          }),
        );
      }
    },
    [el, geometryOf, thumbOffsetOf],
  );

  const endDrag = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    dragRef.current = null;
    setDragging(false);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }, []);

  if (!el || !scrollable) return null;

  return (
    // The track never takes the pointer: it overlays the transcript's right
    // edge, where swallowing clicks would break text selection. Only the thumb
    // is grabbable, which is all a drag needs.
    <div
      aria-hidden
      data-testid="transcript-scrollbar"
      data-transcript-scrollbar=""
      className="pointer-events-none absolute right-1 z-10 w-3"
      style={{ top: topInset, bottom: TRACK_BOTTOM_PX }}
    >
      <div
        data-testid="transcript-scrollbar-thumb"
        data-transcript-scrollbar=""
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        className={cn(
          // touch-none: the drag is pointer-event driven, so the browser's
          // native touch-pan arbitration must not steal the gesture (it fires
          // pointercancel right after pointerdown and the drag dies).
          "pointer-events-auto absolute right-0.5 w-1.5 cursor-default touch-none rounded-full",
          "bg-foreground/20 transition-[width,background-color] duration-150",
          "hover:w-2.5 hover:bg-foreground/40",
          dragging && "w-2.5 bg-foreground/50",
        )}
        style={{ height: thumbPx, transform: `translateY(${offset}px)` }}
      />
    </div>
  );
}
