// Slide-deck viewer for `*.slides.html`: one top-level <section> per slide,
// rendered in the same sandboxed srcdoc iframe as the HTML preview. The parent
// only talks to the deck over postMessage (the iframe has an opaque origin).

import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  CodeIcon,
  Maximize2Icon,
  Minimize2Icon,
  PaletteIcon,
  PresentationIcon,
  PrinterIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { fetchFileContent } from "@/hooks/useFileContent";
import { cn } from "@/lib/utils";
import {
  HTML_PREVIEW_SANDBOX,
  SLIDES_EDITABLE_SELECTOR,
  SLIDES_MSG_SOURCE,
  countSlideSections,
  loadDesignKit,
  prepareSlidesDoc,
  type DesignKitState,
} from "./codeViewerHelpers";
import { TruncatedBanner } from "./TruncatedBanner";

// Fixed 16:9 stage; the iframe renders at this size and is scaled to fit.
const STAGE_W = 1280;
const STAGE_H = 720;
/** Upper bound for iframe-reported slide counts; anything above is clamped. */
export const MAX_SLIDE_COUNT = 1000;

const PREV_KEYS = new Set(["ArrowLeft", "PageUp"]);
const NEXT_KEYS = new Set(["ArrowRight", "PageDown"]);

/** Same rule as the iframe script: leave modified keys and editable targets alone. */
export function isIgnoredNavKey(e: {
  defaultPrevented: boolean;
  altKey: boolean;
  metaKey: boolean;
  ctrlKey: boolean;
  target: EventTarget | null;
}): boolean {
  if (e.defaultPrevented || e.altKey || e.metaKey || e.ctrlKey) return true;
  return e.target instanceof Element && !!e.target.closest(SLIDES_EDITABLE_SELECTOR);
}

// A stalled kit read must not leave the deck blank.
export const DESIGN_KIT_TIMEOUT_MS = 2000;
const NO_KIT: DesignKitState = { status: "none" };
const KIT_TIMED_OUT: DesignKitState = { status: "error", reason: "design kit timed out" };

/** Workspace file read for the kit loader; a 404 means "no such file". */
async function readKitFile(conversationId: string, path: string) {
  try {
    return await fetchFileContent(conversationId, path);
  } catch (e) {
    if (e instanceof Error && e.message.startsWith("404")) return null;
    throw e;
  }
}

export interface SlidesViewerProps {
  content: string;
  truncated?: boolean;
  /** Session whose workspace may hold `.omnigent/design-kit/`. */
  conversationId?: string;
  /** Switches the file viewer to the existing source view. */
  onRequestSourceMode?: () => void;
}

export function SlidesViewer({
  content,
  truncated = false,
  conversationId,
  onRequestSourceMode,
}: SlidesViewerProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const sourceTotal = useMemo(() => countSlideSections(content), [content]);
  // Hold the deck blank until this session's kit resolves so it never flashes
  // unbranded; a kit loaded for another session counts as not loaded.
  const [loaded, setLoaded] = useState<{ id: string; kit: DesignKitState } | null>(null);
  const kit = !conversationId ? NO_KIT : loaded?.id === conversationId ? loaded.kit : null;
  useEffect(() => {
    if (!conversationId) return;
    let cancelled = false;
    const finish = (k: DesignKitState) => {
      if (cancelled) return;
      cancelled = true;
      setLoaded({ id: conversationId, kit: k });
    };
    const timer = setTimeout(() => finish(KIT_TIMED_OUT), DESIGN_KIT_TIMEOUT_MS);
    void loadDesignKit((p) => readKitFile(conversationId, p)).then(finish);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [conversationId, content]);
  const kitReady = kit !== null;
  const kitStyle = kit?.status === "ok" ? kit.style : "";
  const srcDoc = useMemo(
    () => (kitReady ? prepareSlidesDoc(content, kitStyle) : ""),
    [content, kitReady, kitStyle],
  );
  // The iframe's runtime count wins once it reports for the current document.
  const [runtime, setRuntime] = useState<{ srcDoc: string; total: number } | null>(null);
  const total = Math.min(runtime?.srcDoc === srcDoc ? runtime.total : sourceTotal, MAX_SLIDE_COUNT);
  const [index, setIndex] = useState(0);
  const [scale, setScale] = useState(0);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const fullscreenSupported = typeof document !== "undefined" && !!document.fullscreenEnabled;

  const current = Math.min(index, Math.max(0, total - 1));
  // Step from the clamped index so a shrinking deck never eats a keypress.
  const step = useCallback(
    (delta: number) =>
      setIndex((i) => {
        const last = Math.max(0, total - 1);
        return Math.max(0, Math.min(last, Math.min(i, last) + delta));
      }),
    [total],
  );

  const post = useCallback((msg: Record<string, unknown>) => {
    iframeRef.current?.contentWindow?.postMessage({ source: SLIDES_MSG_SOURCE, ...msg }, "*");
  }, []);

  useEffect(() => post({ type: "goto", index: current }), [current, post]);

  // Count reports and forwarded keys arrive as messages; trust only our iframe.
  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      const win = iframeRef.current?.contentWindow;
      if (!win || e.source !== win || e.data?.source !== SLIDES_MSG_SOURCE) return;
      const { type, key, total: n } = e.data;
      // Non-negative integers only; the shared total clamp caps both source and runtime.
      if (type === "count" && Number.isInteger(n) && n >= 0) setRuntime({ srcDoc, total: n });
      else if (type === "key" && PREV_KEYS.has(key)) step(-1);
      else if (type === "key" && NEXT_KEYS.has(key)) step(1);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [step, srcDoc]);

  // Scale the stage to fit the container, letterboxed (desktop rail and mobile).
  useEffect(() => {
    const el = stageRef.current;
    if (!el) return;
    const measure = () =>
      setScale(Math.min(el.clientWidth / STAGE_W, el.clientHeight / STAGE_H) || 0);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    const onChange = () => setIsFullscreen(document.fullscreenElement === rootRef.current);
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);

  const toggleFullscreen = () => {
    const op = document.fullscreenElement
      ? document.exitFullscreen()
      : rootRef.current?.requestFullscreen();
    op?.catch(() => {});
  };

  const onKeyDown = (e: KeyboardEvent) => {
    if (isIgnoredNavKey(e)) return;
    if (PREV_KEYS.has(e.key)) step(-1);
    else if (NEXT_KEYS.has(e.key)) step(1);
    else return;
    e.preventDefault();
  };

  const sourceButton = onRequestSourceMode && (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      aria-label="View deck source"
      title="View deck source"
      onClick={onRequestSourceMode}
      className="h-8 gap-1.5 px-2"
    >
      <CodeIcon className="size-4" />
      Source
    </Button>
  );

  const empty = total === 0;

  // One tree for both states so the iframe stays mounted (hidden) while empty
  // and can still report a runtime count.
  return (
    <div
      ref={rootRef}
      tabIndex={0}
      role="region"
      aria-roledescription="slide deck"
      aria-label="Slide deck"
      onKeyDown={onKeyDown}
      className="flex h-full flex-col bg-background outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-ring"
    >
      {truncated && <TruncatedBanner />}
      {kit?.status === "error" && (
        <div
          role="status"
          className="shrink-0 truncate border-b border-border bg-muted px-3 py-1 text-ui text-muted-foreground"
          title={kit.reason}
        >
          Design kit not applied: {kit.reason}
        </div>
      )}
      {empty && (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 p-8 text-center text-ui text-muted-foreground">
          <PresentationIcon className="size-6" />
          <p className="font-medium text-foreground">No slides yet</p>
          <p>Add a top-level &lt;section&gt; for each slide.</p>
          {sourceButton}
        </div>
      )}
      <div
        ref={stageRef}
        className={cn(
          "relative flex min-h-0 flex-1 items-center justify-center overflow-hidden bg-muted",
          empty && "hidden",
        )}
      >
        <div style={{ width: STAGE_W * scale, height: STAGE_H * scale }}>
          <iframe
            ref={iframeRef}
            srcDoc={srcDoc}
            sandbox={HTML_PREVIEW_SANDBOX}
            title="Slide deck"
            onLoad={() => post({ type: "goto", index: current })}
            className="origin-top-left border-0 bg-white"
            style={{ width: STAGE_W, height: STAGE_H, transform: `scale(${scale})` }}
          />
        </div>
      </div>
      {!empty && (
        <div className="flex shrink-0 items-center justify-between gap-2 border-t border-border px-2 py-1 text-ui text-muted-foreground">
          <div className="flex items-center gap-1">
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label="Previous slide"
              disabled={current === 0}
              onClick={() => step(-1)}
              className="size-8"
            >
              <ChevronLeftIcon className="size-4" />
            </Button>
            <span aria-live="polite" className="min-w-12 text-center tabular-nums text-foreground">
              {current + 1} / {total}
            </span>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label="Next slide"
              disabled={current === total - 1}
              onClick={() => step(1)}
              className="size-8"
            >
              <ChevronRightIcon className="size-4" />
            </Button>
          </div>
          <div className="flex min-w-0 items-center gap-1">
            {kit?.status === "ok" && (
              <span
                className="flex min-w-0 items-center gap-1 px-1"
                title={`Design kit: ${kit.name}`}
              >
                <PaletteIcon className="size-3.5 shrink-0" aria-hidden />
                <span className="max-w-32 truncate max-sm:sr-only">{kit.name}</span>
              </span>
            )}
            <Button
              type="button"
              variant="ghost"
              size="sm"
              aria-label="Print / Save as PDF"
              title="Print / Save as PDF"
              onClick={() => post({ type: "print" })}
              className="h-8 gap-1.5 px-2"
            >
              <PrinterIcon className="size-4" />
              <span className="hidden sm:inline">Print / Save as PDF</span>
            </Button>
            {sourceButton}
            {fullscreenSupported && (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={isFullscreen ? "Exit fullscreen" : "Enter fullscreen"}
                title={isFullscreen ? "Exit fullscreen" : "Enter fullscreen"}
                onClick={toggleFullscreen}
                className="size-8"
              >
                {isFullscreen ? (
                  <Minimize2Icon className="size-4" />
                ) : (
                  <Maximize2Icon className="size-4" />
                )}
              </Button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
