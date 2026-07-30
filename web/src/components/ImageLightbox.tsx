import * as React from "react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Dialog as DialogPrimitive } from "radix-ui";
import { XIcon, ZoomInIcon, ZoomOutIcon } from "lucide-react";

import { getEmbedRoot } from "@/lib/host";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

// Zoom bounds and step for the lightbox viewer. 1 = fit-to-card.
const MIN_ZOOM = 1;
const MAX_ZOOM = 8;
const ZOOM_STEP = 0.25;

function clampZoom(z: number) {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z));
}

interface LightboxImage {
  src: string;
  alt: string;
}

interface LightboxContextValue {
  open: (image: LightboxImage) => void;
}

// No-op fallback so image components keep working (just non-zoomable) when
// rendered outside the provider — e.g. in isolated unit tests.
const NOOP: LightboxContextValue = { open: () => {} };

const LightboxContext = createContext<LightboxContextValue | null>(null);

export function useLightbox(): LightboxContextValue {
  return useContext(LightboxContext) ?? NOOP;
}

export interface ZoomableImageProps extends React.ComponentProps<"img"> {
  /** Display source. May be undefined while the image is still resolving. */
  src?: string;
  alt: string;
}

/**
 * An `<img>` wrapped in a `<button>` that opens it in the shared lightbox. The
 * button carries the interaction (click + Enter/Space + focus ring, all native
 * to a button); the inner `<img>` keeps its `role="img"` and alt-derived name,
 * so screen readers and tests still see a real image. Activation is a no-op
 * until `src` resolves. `className` styles the inner `<img>` (sizing,
 * `object-contain`, etc.) — the button is a layout-transparent wrapper.
 */
export function ZoomableImage({ src, alt, className, ...imgProps }: ZoomableImageProps) {
  const { open } = useLightbox();
  return (
    <button
      type="button"
      aria-label={alt ? `Zoom image: ${alt}` : "Zoom image"}
      className="m-0 inline-flex max-w-full cursor-zoom-in appearance-none border-0 bg-transparent p-0 leading-none"
      onClick={() => {
        if (src) open({ src, alt });
      }}
    >
      <img {...imgProps} src={src} alt={alt} className={className} />
    </button>
  );
}

/**
 * The zoomable image inside the lightbox card. Holds its own zoom/pan state and
 * is keyed by `src` in the provider so that state resets per image.
 *
 * Zoom: scroll wheel (anchored at the cursor), the +/- toolbar buttons, or
 * double-click to toggle between fit and 2x. Pan: drag while zoomed in. The
 * image is `object-contain` within a fixed-size viewport that clips overflow,
 * so the zoomed image never escapes the card.
 */
function ZoomViewer({ image }: { image: LightboxImage }) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  // Active pointer drag (panning); null when not dragging.
  const dragRef = useRef<{ pointerId: number; startX: number; startY: number } | null>(null);

  const resetView = useCallback(() => {
    setZoom(1);
    setOffset({ x: 0, y: 0 });
  }, []);

  const applyZoom = useCallback((next: number) => setZoom(clampZoom(next)), []);

  // Wheel-to-zoom. Attached as a non-passive native listener so preventDefault
  // works (React's onWheel is passive and would warn), keeping the page behind
  // the modal from scrolling while zooming.
  useEffect(() => {
    const el = viewportRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      setZoom((z) => clampZoom(z - Math.sign(e.deltaY) * ZOOM_STEP * 2));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  // Drop pan offset whenever zoom returns to fit.
  useEffect(() => {
    if (zoom === MIN_ZOOM) setOffset({ x: 0, y: 0 });
  }, [zoom]);

  const onPointerDown = (e: React.PointerEvent) => {
    if (zoom <= MIN_ZOOM) return;
    dragRef.current = {
      pointerId: e.pointerId,
      startX: e.clientX - offset.x,
      startY: e.clientY - offset.y,
    };
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const drag = dragRef.current;
    if (!drag) return;
    setOffset({ x: e.clientX - drag.startX, y: e.clientY - drag.startY });
  };
  const endDrag = (e: React.PointerEvent) => {
    if (dragRef.current?.pointerId === e.pointerId) dragRef.current = null;
  };

  const zoomed = zoom > MIN_ZOOM;

  return (
    <>
      <div
        ref={viewportRef}
        className="absolute inset-0 flex items-center justify-center overflow-hidden"
        onDoubleClick={() => applyZoom(zoomed ? MIN_ZOOM : 2)}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        style={{ cursor: zoomed ? (dragRef.current ? "grabbing" : "grab") : "zoom-in" }}
      >
        <img
          src={image.src}
          alt={image.alt}
          draggable={false}
          className="max-h-[92vh] max-w-[94vw] origin-center object-contain select-none"
          style={{
            transform: `translate(${offset.x}px, ${offset.y}px) scale(${zoom})`,
            // Don't fight the drag with a transition while panning, but ease
            // discrete zoom steps.
            transition: dragRef.current ? "none" : "transform 120ms ease-out",
          }}
        />
      </div>
      {/* Zoom toolbar — bottom-center pill. */}
      <div className="absolute bottom-3 left-1/2 flex -translate-x-1/2 items-center gap-1 rounded-full bg-background/80 p-1 shadow-sm ring-1 ring-foreground/10 backdrop-blur-xs">
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Zoom out"
          disabled={zoom <= MIN_ZOOM}
          onClick={() => applyZoom(zoom - ZOOM_STEP * 2)}
        >
          <ZoomOutIcon />
        </Button>
        <button
          type="button"
          aria-label="Reset zoom"
          className="min-w-[3ch] cursor-pointer text-center text-xs tabular-nums text-muted-foreground hover:text-foreground"
          onClick={resetView}
        >
          {Math.round(zoom * 100)}%
        </button>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Zoom in"
          disabled={zoom >= MAX_ZOOM}
          onClick={() => applyZoom(zoom + ZOOM_STEP * 2)}
        >
          <ZoomInIcon />
        </Button>
      </div>
    </>
  );
}

/**
 * Provides a single shared full-screen image viewer for the whole app. Any
 * image wired with {@link useImageZoomProps} opens here. Built on the Radix
 * Dialog primitive, so Escape closes it for free; an explicit "x" button gives
 * the second close affordance. The content fills the viewport, so a click never
 * lands "outside" — closing is Escape or the x only, by design.
 */
export function ImageLightboxProvider({ children }: { children: React.ReactNode }) {
  const [image, setImage] = useState<LightboxImage | null>(null);

  const open = useCallback((img: LightboxImage) => setImage(img), []);
  const value = useMemo(() => ({ open }), [open]);

  return (
    <LightboxContext.Provider value={value}>
      {children}
      <DialogPrimitive.Root
        open={image !== null}
        onOpenChange={(next) => {
          if (!next) setImage(null);
        }}
      >
        <DialogPrimitive.Portal container={getEmbedRoot() ?? undefined}>
          {/* Dark backdrop — dims the whole page to focus on the preview. */}
          <DialogPrimitive.Overlay
            className={cn(
              "fixed inset-0 z-[60] bg-black/80 duration-150 ease-[cubic-bezier(0.16,1,0.3,1)]",
              "data-open:animate-in data-open:fade-in-0 data-closed:animate-out data-closed:fade-out-0",
            )}
          />
          {/* Full-screen stage (Slack-style): the image sits centered on the
              dark backdrop and can zoom to fill the whole viewport, clipped to
              the screen rather than to a small card. */}
          <DialogPrimitive.Content
            className={cn(
              "fixed inset-0 z-[60] outline-none",
              "duration-150 ease-[cubic-bezier(0.16,1,0.3,1)]",
              "data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95",
              "data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95",
            )}
            // The image carries its own description; the title is for a11y only.
            aria-describedby={undefined}
            // Close is Escape or the "x" only — keep clicks on the backdrop
            // (and elsewhere) from dismissing the preview.
            onInteractOutside={(e) => e.preventDefault()}
          >
            <DialogPrimitive.Title className="sr-only">
              {image?.alt || "Image preview"}
            </DialogPrimitive.Title>
            {/* key by src so zoom/pan state resets when a new image opens. */}
            {image && <ZoomViewer key={image.src} image={image} />}
            <DialogPrimitive.Close asChild>
              <Button
                variant="ghost"
                size="icon-sm"
                className="absolute top-3 right-3 bg-background/70 hover:bg-background/90"
              >
                <XIcon />
                <span className="sr-only">Close</span>
              </Button>
            </DialogPrimitive.Close>
          </DialogPrimitive.Content>
        </DialogPrimitive.Portal>
      </DialogPrimitive.Root>
    </LightboxContext.Provider>
  );
}
