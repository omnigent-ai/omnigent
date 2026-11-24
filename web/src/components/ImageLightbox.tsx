import * as React from "react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { flushSync } from "react-dom";
import * as DialogPrimitive from "radix-ui/dialog";
import { DownloadIcon, XIcon, ZoomInIcon, ZoomOutIcon } from "lucide-react";
import { toast } from "sonner";

import { getEmbedRoot } from "@/lib/host";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { canDownloadImage, downloadImage } from "@/hooks/useFileContent";
import { isAndroidShell, isIOSShell } from "@/lib/nativeBridge";
import { cn } from "@/lib/utils";

// Zoom bounds and step for the lightbox viewer. 1 = fit-to-card.
const MIN_ZOOM = 1;
const MAX_ZOOM = 8;
const ZOOM_STEP = 0.25;

/** Pointer travel that separates a pan from a click, in CSS pixels. */
const DRAG_SLOP_PX = 4;

/** How far two fingers must spread over a chat image, as a multiple of their starting gap, to open it. */
const PINCH_OPEN_RATIO = 1.2;

function clampZoom(z: number) {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z));
}

function clampMagnitude(value: number, limit: number) {
  return Math.min(limit, Math.max(-limit, value));
}

function fingerGap(touches: TouchList) {
  return Math.hypot(
    touches[1].clientX - touches[0].clientX,
    touches[1].clientY - touches[0].clientY,
  );
}

interface LightboxImage {
  src: string;
  alt: string;
  /** Save-dialog name; defaults to `alt`. */
  downloadName?: string;
  /** App API path the image was read from, for sources the page can't save itself. */
  downloadPath?: string;
  /** The image's bytes; outlives an object URL its owner revokes while the preview is open. */
  downloadBlob?: Blob;
}

function ImageDownloadButton({ image, className }: { image: LightboxImage; className?: string }) {
  const [saving, setSaving] = useState(false);
  // A ref, not state, so two clicks in one frame can't both pass the check.
  const savingRef = useRef(false);
  const name = image.downloadName || image.alt;
  const save = () => {
    if (savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    downloadImage(image.src, name || "image", {
      apiPath: image.downloadPath,
      blob: image.downloadBlob,
    })
      .catch(() => toast.error("Download failed"))
      .finally(() => {
        savingRef.current = false;
        setSaving(false);
      });
  };
  return (
    // Tests and stories render images without the app root's provider.
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label={name ? `Download image: ${name}` : "Download image"}
            className={className}
            disabled={saving}
            onClick={(e) => {
              e.stopPropagation();
              save();
            }}
          >
            <DownloadIcon />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">Download</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
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
  /** Optional sizing for the button around the image. */
  buttonClassName?: string;
  /** Save-dialog name; setting it also shows a download button on hover. */
  downloadName?: string;
  /** App API path the image was read from, for sources the page can't save itself. */
  downloadPath?: string;
  /** The image's bytes, so a save survives its owner revoking `src`. */
  downloadBlob?: Blob;
}

/**
 * An `<img>` wrapped in a `<button>` that opens it in the shared lightbox. The
 * button carries the interaction (click + Enter/Space + focus ring, all native
 * to a button); the inner `<img>` keeps its `role="img"` and alt-derived name,
 * so screen readers and tests still see a real image. Activation is a no-op
 * until `src` resolves. `className` styles the image; `buttonClassName` can
 * reserve a stable hit target around a smaller image.
 */
export function ZoomableImage({
  src,
  alt,
  className,
  buttonClassName,
  downloadName,
  downloadPath,
  downloadBlob,
  ...imgProps
}: ZoomableImageProps) {
  const { open } = useLightbox();
  const buttonRef = useRef<HTMLButtonElement>(null);

  // The native shells lock page zoom, so a pinch over the image opens the
  // preview, which keeps zooming with the same fingers.
  useEffect(() => {
    const button = buttonRef.current;
    if (!button || !src || !(isIOSShell() || isAndroidShell())) return;
    let startGap: number | null = null;
    const onTouch = (e: TouchEvent) => {
      if (e.touches.length !== 2) {
        startGap = null;
        return;
      }
      if (e.cancelable) e.preventDefault();
      const gap = fingerGap(e.touches);
      if (startGap === null) {
        startGap = gap;
      } else if (gap >= startGap * PINCH_OPEN_RATIO) {
        // Open once per pinch, and mount the preview before the next move arrives.
        startGap = Infinity;
        flushSync(() => open({ src, alt, downloadName, downloadPath, downloadBlob }));
      }
    };
    button.addEventListener("touchstart", onTouch, { passive: false });
    button.addEventListener("touchmove", onTouch, { passive: false });
    button.addEventListener("touchend", onTouch);
    button.addEventListener("touchcancel", onTouch);
    return () => {
      button.removeEventListener("touchstart", onTouch);
      button.removeEventListener("touchmove", onTouch);
      button.removeEventListener("touchend", onTouch);
      button.removeEventListener("touchcancel", onTouch);
    };
  }, [open, src, alt, downloadName, downloadPath, downloadBlob]);

  const zoom = (
    <button
      ref={buttonRef}
      type="button"
      aria-label={alt ? `Zoom image: ${alt}` : "Zoom image"}
      className={cn(
        "m-0 inline-flex max-w-full cursor-zoom-in appearance-none border-0 bg-transparent p-0 leading-none",
        buttonClassName,
      )}
      onClick={() => {
        if (src) open({ src, alt, downloadName, downloadPath, downloadBlob });
      }}
    >
      <img {...imgProps} src={src} alt={alt} className={className} />
    </button>
  );
  const source = { apiPath: downloadPath, blob: downloadBlob };
  if (!downloadName || !src || !canDownloadImage(src, source)) return zoom;
  return (
    <span className="group/image relative inline-flex max-w-full">
      {zoom}
      <ImageDownloadButton
        image={{ src, alt, downloadName, downloadPath, downloadBlob }}
        className={cn(
          "absolute top-1 right-1 bg-background/80 shadow-sm ring-1 ring-foreground/10 backdrop-blur-xs hover:bg-background/90",
          // Touch screens never hover, so the hidden button must not take the zoom tap.
          "pointer-events-none opacity-0 group-hover/image:pointer-events-auto group-hover/image:opacity-100 focus-visible:pointer-events-auto focus-visible:opacity-100",
        )}
      />
    </span>
  );
}

/**
 * The zoomable image inside the lightbox card. Holds its own zoom/pan state and
 * is keyed by `src` in the provider so that state resets per image.
 *
 * Zoom: scroll wheel (anchored at the cursor), the +/- toolbar buttons,
 * double-click to toggle between fit and 2x, or a two-finger pinch (anchored
 * between the fingers). Pan: drag while zoomed in. The
 * image is `object-contain` within a fixed-size viewport that clips overflow,
 * so the zoomed image never escapes the card. A click on the empty stage
 * around the image dismisses the preview.
 */
function ZoomViewer({ image, onClose }: { image: LightboxImage; onClose: () => void }) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const imageRef = useRef<HTMLImageElement>(null);
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  // Active pointer drag (panning); null when not dragging.
  const dragRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    originX: number;
    originY: number;
  } | null>(null);
  // Whether the current gesture panned, so its closing click isn't a dismiss.
  const pannedRef = useRef(false);
  // Active two-finger pinch; the anchor is the image point between the fingers, at scale 1.
  const pinchRef = useRef<{
    startGap: number;
    startZoom: number;
    anchorX: number;
    anchorY: number;
  } | null>(null);
  const viewRef = useRef({ zoom, offset });
  useLayoutEffect(() => {
    viewRef.current = { zoom, offset };
  });

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

  // Pinch-to-zoom. Listens on the document, not the stage, so a pinch begun on
  // a chat image carries on into the preview it opened.
  useEffect(() => {
    const onTouch = (e: TouchEvent) => {
      const stage = viewportRef.current;
      // A finger landing or lifting re-anchors the pinch on the current pair.
      if (e.type !== "touchmove") pinchRef.current = null;
      if (!stage || e.touches.length < 2) {
        pinchRef.current = null;
        return;
      }
      if (e.cancelable) e.preventDefault();
      const rect = stage.getBoundingClientRect();
      const midX = (e.touches[0].clientX + e.touches[1].clientX) / 2 - rect.left - rect.width / 2;
      const midY = (e.touches[0].clientY + e.touches[1].clientY) / 2 - rect.top - rect.height / 2;
      const gap = fingerGap(e.touches);
      const pinch = pinchRef.current;
      if (!pinch) {
        const { zoom: startZoom, offset: startOffset } = viewRef.current;
        // Fingers beside the image (or over the chat image that opened it) hold
        // the nearest edge, so the zoom can't push the image off-screen.
        const halfWidth = (imageRef.current?.offsetWidth ?? 0) / 2;
        const halfHeight = (imageRef.current?.offsetHeight ?? 0) / 2;
        // The pinch replaces any one-finger pan, and its release is not a dismiss.
        dragRef.current = null;
        pannedRef.current = true;
        pinchRef.current = {
          startGap: Math.max(gap, 1),
          startZoom,
          anchorX: clampMagnitude((midX - startOffset.x) / startZoom, halfWidth),
          anchorY: clampMagnitude((midY - startOffset.y) / startZoom, halfHeight),
        };
        return;
      }
      const next = clampZoom((pinch.startZoom * gap) / pinch.startGap);
      setZoom(next);
      setOffset(
        next === MIN_ZOOM
          ? { x: 0, y: 0 }
          : { x: midX - next * pinch.anchorX, y: midY - next * pinch.anchorY },
      );
    };
    document.addEventListener("touchstart", onTouch, { passive: false });
    document.addEventListener("touchmove", onTouch, { passive: false });
    document.addEventListener("touchend", onTouch);
    document.addEventListener("touchcancel", onTouch);
    return () => {
      document.removeEventListener("touchstart", onTouch);
      document.removeEventListener("touchmove", onTouch);
      document.removeEventListener("touchend", onTouch);
      document.removeEventListener("touchcancel", onTouch);
    };
  }, []);

  // Drop pan offset whenever zoom returns to fit.
  useEffect(() => {
    if (zoom === MIN_ZOOM) setOffset({ x: 0, y: 0 });
  }, [zoom]);

  const onPointerDown = (e: React.PointerEvent) => {
    if (pinchRef.current) return;
    pannedRef.current = false;
    if (zoom <= MIN_ZOOM) return;
    dragRef.current = {
      pointerId: e.pointerId,
      startX: e.clientX - offset.x,
      startY: e.clientY - offset.y,
      originX: e.clientX,
      originY: e.clientY,
    };
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const drag = dragRef.current;
    if (!drag) return;
    if (Math.hypot(e.clientX - drag.originX, e.clientY - drag.originY) > DRAG_SLOP_PX) {
      pannedRef.current = true;
    }
    setOffset({ x: e.clientX - drag.startX, y: e.clientY - drag.startY });
  };
  const endDrag = (e: React.PointerEvent) => {
    if (dragRef.current?.pointerId === e.pointerId) dragRef.current = null;
  };

  // A click on the stage dismisses, but only a real one: a click whose target
  // is the image (or the toolbar) is not "outside", and the click a pan leaves
  // behind when the pointer is released would otherwise close the preview
  // every time someone repositions a zoomed image.
  const onStageClick = (e: React.MouseEvent) => {
    if (e.target !== e.currentTarget || pannedRef.current) return;
    onClose();
  };

  const zoomed = zoom > MIN_ZOOM;

  return (
    <>
      <div
        ref={viewportRef}
        className="absolute inset-0 flex touch-none items-center justify-center overflow-hidden"
        onClick={onStageClick}
        onDoubleClick={() => applyZoom(zoomed ? MIN_ZOOM : 2)}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        style={{ cursor: zoomed ? (dragRef.current ? "grabbing" : "grab") : "zoom-in" }}
      >
        <img
          ref={imageRef}
          src={image.src}
          alt={image.alt}
          draggable={false}
          className="max-h-[92vh] max-w-[94vw] origin-center object-contain select-none"
          style={{
            transform: `translate(${offset.x}px, ${offset.y}px) scale(${zoom})`,
            // Don't fight a drag or pinch with a transition, but ease discrete
            // zoom steps.
            transition: dragRef.current || pinchRef.current ? "none" : "transform 120ms ease-out",
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
          className="min-w-[3ch] cursor-pointer text-center text-sm tabular-nums text-muted-foreground hover:text-foreground"
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
        {canDownloadImage(image.src, { apiPath: image.downloadPath, blob: image.downloadBlob }) && (
          <ImageDownloadButton image={image} />
        )}
      </div>
    </>
  );
}

/**
 * Provides a single shared full-screen image viewer for the whole app. Any
 * image wired with {@link useImageZoomProps} opens here. Built on the Radix
 * Dialog primitive, so Escape closes it for free; an explicit "x" button and a
 * click on the empty stage around the image give the other two close
 * affordances.
 */
export function ImageLightboxProvider({ children }: { children: React.ReactNode }) {
  const [image, setImage] = useState<LightboxImage | null>(null);

  const open = useCallback((img: LightboxImage) => setImage(img), []);
  const close = useCallback(() => setImage(null), []);
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
            // The stage fills the viewport, so no click is ever "outside" it
            // as far as Radix is concerned; dismissing on a click around the
            // image is the stage's own handler, which can tell a click on the
            // image or a finished pan from a real one.
            onInteractOutside={(e) => e.preventDefault()}
          >
            <DialogPrimitive.Title className="sr-only">
              {image?.alt || "Image preview"}
            </DialogPrimitive.Title>
            {/* key by src so zoom/pan state resets when a new image opens. */}
            {image && <ZoomViewer key={image.src} image={image} onClose={close} />}
            <DialogPrimitive.Close asChild>
              <Button
                variant="ghost"
                size="icon-sm"
                // Offset by the top safe-area inset so the button clears the
                // status bar / notch on native shells and stays tappable.
                style={{
                  top: "calc(0.75rem + var(--omnigent-safe-top, 0px))",
                  right: "calc(0.75rem + var(--omnigent-safe-right, 0px))",
                }}
                className="absolute bg-background/70 hover:bg-background/90"
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
