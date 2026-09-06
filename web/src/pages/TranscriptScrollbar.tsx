// A constant-height scrollbar for the transcript.
//
// The native bar can't sit still here: the transcript is lazily paginated, so
// scrolling up genuinely lengthens the document and the thumb shrinks a step
// per page — motion the reader reads as the scrollbar bouncing under their
// hand. A proportional thumb is also lying while that is happening, since the
// document it is measuring is only the part fetched so far. This one reports
// position and nothing else, so its size never changes.

import { useCallback, useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";

/** The scroll container plus the bottom-lock release, as lifted by ChatPage. */
interface Scroller {
  el: HTMLElement;
  stopScroll: () => void;
}

/** Thumb height. Constant — that is the whole point of this component. */
const THUMB_PX = 56;
/**
 * Smallest scroll range worth a thumb. Fractional content heights round into
 * `scrollHeight`, and LatestTurnSpacer's 1px write-hysteresis can leave the
 * document a couple of pixels taller than the viewport — painting a thumb for
 * that noise advertises hidden content that does not exist. The trade: a real
 * range under 4px also goes unindicated — imperceptible at these sizes.
 */
const MIN_SCROLL_RANGE_PX = 4;
/** Track inset from the top, clearing the ChatHeader overlay's controls. */
const TRACK_TOP_PX = 64;
const TRACK_BOTTOM_PX = 12;

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
  const [scrollable, setScrollable] = useState(false);
  const [dragging, setDragging] = useState(false);
  // Captured on drag start; the thumb maps its travel onto the full scroll
  // range, so a drag stays anchored to where it was grabbed.
  const dragRef = useRef<{ pointerY: number; scrollTop: number } | null>(null);

  const el = scroller?.el ?? null;

  /** Usable thumb travel: the track minus the thumb's own height. */
  const travelOf = useCallback(
    (node: HTMLElement) => node.clientHeight - topInset - TRACK_BOTTOM_PX - THUMB_PX,
    [topInset],
  );

  useEffect(() => {
    if (!el) return;
    const measure = () => {
      const max = el.scrollHeight - el.clientHeight;
      const travel = travelOf(el);
      if (max < MIN_SCROLL_RANGE_PX || travel <= 0) {
        setScrollable(false);
        return;
      }
      setScrollable(true);
      setOffset(Math.round(Math.min(1, Math.max(0, el.scrollTop / max)) * travel));
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
  }, [el, travelOf]);

  const onPointerDown = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      if (!el || !scroller) return;
      event.preventDefault();
      event.currentTarget.setPointerCapture(event.pointerId);
      // StickToBottom re-pins to the bottom on content resize unless the lock
      // is released, which would fight a drag away from the newest turn.
      scroller.stopScroll();
      dragRef.current = { pointerY: event.clientY, scrollTop: el.scrollTop };
      setDragging(true);
    },
    [el, scroller],
  );

  const onPointerMove = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      const drag = dragRef.current;
      if (!drag || !el) return;
      const travel = travelOf(el);
      if (travel <= 0) return;
      const max = el.scrollHeight - el.clientHeight;
      el.scrollTop = drag.scrollTop + ((event.clientY - drag.pointerY) / travel) * max;
    },
    [el, travelOf],
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
      className="pointer-events-none absolute right-1 z-10 w-3"
      style={{ top: topInset, bottom: TRACK_BOTTOM_PX }}
    >
      <div
        data-testid="transcript-scrollbar-thumb"
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
        style={{ height: THUMB_PX, transform: `translateY(${offset}px)` }}
      />
    </div>
  );
}
