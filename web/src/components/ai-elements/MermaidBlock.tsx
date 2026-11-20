import { Button } from "@/components/ui/button";
import { getEmbedRoot } from "@/lib/host";
import { cn } from "@/lib/utils";
import type {
  ComponentProps,
  PointerEvent as ReactPointerEvent,
  ReactNode,
  RefObject,
} from "react";
import { cloneElement, isValidElement, useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

// Streamdown portals fullscreen to document.body, outside the embed's scoped CSS.
export const MERMAID_NO_FULLSCREEN = { fullscreen: false } as const;

// Reproduced from Streamdown 2.5's own "View fullscreen" / "Exit fullscreen"
// glyphs so our button matches the download/copy icons in the same mermaid bar.
export const FullscreenIcon = (props: ComponentProps<"svg">) => (
  <svg fill="none" height={16} viewBox="0 0 16 16" width={16} {...props}>
    <path
      clipRule="evenodd"
      d="M1 5.25V6H2.5V5.25V2.5H5.25H6V1H5.25H2C1.44772 1 1 1.44772 1 2V5.25ZM5.25 14.9994H6V13.4994H5.25H2.5V10.7494V9.99939H1V10.7494V13.9994C1 14.5517 1.44772 14.9994 2 14.9994H5.25ZM15 10V10.75V14C15 14.5523 14.5523 15 14 15H10.75H10V13.5H10.75H13.5V10.75V10H15ZM10.75 1H10V2.5H10.75H13.5V5.25V6H15V5.25V2C15 1.44772 14.5523 1 14 1H10.75Z"
      fill="currentColor"
      fillRule="evenodd"
    />
  </svg>
);

export function isMermaidPre(children: ReactNode): boolean {
  return (
    isValidElement<{ className?: string }>(children) &&
    (children.props.className?.split(/\s+/).includes("language-mermaid") ?? false)
  );
}

const MIN_SCALE = 0.5;
const MAX_SCALE = 4;
const ZOOM_STEP = 1.25;
const WHEEL_NOTCH_PX = 100;
const clampScale = (n: number) => Math.min(MAX_SCALE, Math.max(MIN_SCALE, n));

const ZoomInIcon = (props: ComponentProps<"svg">) => (
  <svg fill="none" height={16} viewBox="0 0 16 16" width={16} {...props}>
    <path d="M8 3v10M3 8h10" stroke="currentColor" strokeLinecap="round" strokeWidth={1.5} />
  </svg>
);
const ZoomOutIcon = (props: ComponentProps<"svg">) => (
  <svg fill="none" height={16} viewBox="0 0 16 16" width={16} {...props}>
    <path d="M3 8h10" stroke="currentColor" strokeLinecap="round" strokeWidth={1.5} />
  </svg>
);
const ResetZoomIcon = (props: ComponentProps<"svg">) => (
  <svg fill="none" height={16} viewBox="0 0 16 16" width={16} {...props}>
    <path
      d="M13 8a5 5 0 1 1-1.6-3.7M13 2.5v2.8h-2.8"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth={1.5}
    />
  </svg>
);

// Reuse the sanitized inline SVG without rerunning Mermaid or copying toolbar icons.
function MermaidFullscreen({ blockRef }: { blockRef: RefObject<HTMLDivElement | null> }) {
  const [svgMarkup, setSvgMarkup] = useState<string | null>(null);
  const close = useCallback(() => setSvgMarkup(null), []);
  const [scale, setScale] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  const dragRef = useRef<{ id: number; x: number; y: number } | null>(null);
  // A drag that ends on empty space fires a click there; it must not dismiss the view.
  const draggedRef = useRef(false);
  const overlayRef = useRef<HTMLDivElement>(null);

  const zoomIn = useCallback(() => setScale((s) => clampScale(s * ZOOM_STEP)), []);
  const zoomOut = useCallback(() => setScale((s) => clampScale(s / ZOOM_STEP)), []);
  const resetZoom = useCallback(() => {
    setScale(1);
    setOffset({ x: 0, y: 0 });
  }, []);

  const onPointerDown = useCallback((e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0 || dragRef.current) return;
    dragRef.current = { id: e.pointerId, x: e.clientX, y: e.clientY };
    draggedRef.current = false;
    e.currentTarget.setPointerCapture?.(e.pointerId);
    setDragging(true);
  }, []);
  const onPointerMove = useCallback((e: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.id !== e.pointerId) return;
    const dx = e.clientX - drag.x;
    const dy = e.clientY - drag.y;
    if (dx === 0 && dy === 0) return;
    draggedRef.current = true;
    dragRef.current = { id: drag.id, x: e.clientX, y: e.clientY };
    setOffset((o) => ({ x: o.x + dx, y: o.y + dy }));
  }, []);
  const endDrag = useCallback((e: ReactPointerEvent<HTMLDivElement>) => {
    if (dragRef.current?.id !== e.pointerId) return;
    dragRef.current = null;
    setDragging(false);
    // Capture is already gone when this runs from lostpointercapture.
    if (e.currentTarget.hasPointerCapture?.(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
  }, []);

  const openFullscreen = useCallback(() => {
    // The rendered diagram is Mermaid's own <svg id="mermaid-…"> — scope to it so
    // we don't clone a toolbar/zoom-control icon (all 16×16 <svg>s in the block).
    const svg = blockRef.current?.querySelector('svg[id^="mermaid-"]');
    setSvgMarkup(svg ? svg.outerHTML : null);
    setScale(1);
    setOffset({ x: 0, y: 0 });
    dragRef.current = null;
    draggedRef.current = false;
    setDragging(false);
  }, [blockRef]);

  useEffect(() => {
    if (svgMarkup == null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [svgMarkup, close]);

  // React's onWheel is passive, so preventDefault needs a native listener.
  useEffect(() => {
    const el = overlayRef.current;
    if (svgMarkup == null || !el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      if (e.deltaY === 0) return;
      // A 100px notch is one step; small trackpad/pinch deltas zoom proportionally.
      const px = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
      const delta = Math.max(-WHEEL_NOTCH_PX, Math.min(WHEEL_NOTCH_PX, px));
      setScale((s) => clampScale(s * ZOOM_STEP ** (-delta / WHEEL_NOTCH_PX)));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [svgMarkup]);

  const overlay =
    svgMarkup != null
      ? createPortal(
          <div
            ref={overlayRef}
            className="fixed inset-0 z-50 flex items-center justify-center bg-background/95 text-foreground backdrop-blur-sm"
            // Clicking the diagram must not dismiss it.
            onClick={(e) => {
              if (e.target === e.currentTarget) close();
            }}
            // Page-wide shortcuts (a pending question's keys) leave Escape to this view.
            data-fullscreen-overlay=""
            role="presentation"
          >
            <div className="absolute top-4 right-14 z-10 flex items-center gap-1">
              <Button
                aria-label="Zoom in"
                onClick={zoomIn}
                size="icon-sm"
                title="Zoom in"
                type="button"
                variant="ghost"
              >
                <ZoomInIcon />
              </Button>
              <Button
                aria-label="Zoom out"
                onClick={zoomOut}
                size="icon-sm"
                title="Zoom out"
                type="button"
                variant="ghost"
              >
                <ZoomOutIcon />
              </Button>
              <Button
                aria-label="Reset zoom"
                onClick={resetZoom}
                size="icon-sm"
                title="Reset zoom"
                type="button"
                variant="ghost"
              >
                <ResetZoomIcon />
              </Button>
            </div>
            <Button
              aria-label="Exit fullscreen"
              className="absolute top-4 right-4 z-10"
              onClick={close}
              size="icon-sm"
              title="Exit fullscreen"
              type="button"
              variant="ghost"
            >
              <FullscreenIcon />
            </Button>
            <div
              className="flex size-full items-center justify-center p-8 [&>svg]:h-auto [&>svg]:max-h-full [&>svg]:w-auto [&>svg]:max-w-full"
              // The cloned SVG is Mermaid's own trusted, already-sanitized render.
              dangerouslySetInnerHTML={{ __html: svgMarkup }}
              data-testid="mermaid-fullscreen-content"
              onClick={(e) => {
                if (e.target === e.currentTarget && !draggedRef.current) close();
              }}
              onLostPointerCapture={endDrag}
              onPointerCancel={endDrag}
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={endDrag}
              role="presentation"
              style={{
                transform: `translate(${offset.x}px, ${offset.y}px) scale(${scale})`,
                cursor: dragging ? "grabbing" : "grab",
                touchAction: "none",
                userSelect: "none",
              }}
            />
          </div>,
          getEmbedRoot() ?? document.body,
        )
      : null;

  return (
    <>
      <Button
        aria-label="View fullscreen"
        className="size-6 bg-sidebar/80 text-muted-foreground hover:bg-sidebar/80 hover:text-foreground dark:hover:bg-sidebar/80 supports-[backdrop-filter]:bg-sidebar/70 supports-[backdrop-filter]:backdrop-blur"
        onClick={openFullscreen}
        size="icon-sm"
        title="View fullscreen"
        type="button"
        variant="ghost"
      >
        <FullscreenIcon />
      </Button>
      {overlay}
    </>
  );
}

// Mermaid's own <svg id="mermaid-…">, or the card that explains why it couldn't draw.
const DRAWN_DIAGRAM = 'svg[id^="mermaid-"], [data-testid="mermaid-error"]';

// The .chat-mermaid rule reserves a slot beside Streamdown's download/copy toolbar.
export function MermaidBlock({
  block,
  className,
  onDrawn,
}: {
  block: ReactNode;
  className?: string;
  onDrawn?: () => void;
}) {
  const blockRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const root = blockRef.current;
    if (!root || !onDrawn) return;
    if (root.querySelector(DRAWN_DIAGRAM)) {
      onDrawn();
      return;
    }
    const observer = new MutationObserver(() => {
      if (root.querySelector(DRAWN_DIAGRAM)) onDrawn();
    });
    observer.observe(root, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [onDrawn]);
  return (
    <div ref={blockRef} className={cn("chat-mermaid relative", className)}>
      {block}
      <div className="absolute top-2 right-2 z-20 flex items-center rounded-md border border-sidebar bg-sidebar/80 px-1.5 py-1 supports-[backdrop-filter]:bg-sidebar/70 supports-[backdrop-filter]:backdrop-blur">
        <MermaidFullscreen blockRef={blockRef} />
      </div>
    </div>
  );
}

function MermaidPre({ children }: ComponentProps<"pre">) {
  // Preserve Streamdown's default handling of ordinary code fences.
  const block = isValidElement(children)
    ? cloneElement(children, { "data-block": "true" } as Record<string, unknown>)
    : children;
  return isMermaidPre(children) ? <MermaidBlock block={block} /> : block;
}

// Keep the replacement toolbar and disabled native fullscreen together on every surface.
export const MERMAID_STREAMDOWN_PROPS = {
  components: { pre: MermaidPre },
  controls: { mermaid: MERMAID_NO_FULLSCREEN },
};
