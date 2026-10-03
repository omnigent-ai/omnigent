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
  PresentationIcon,
  PrinterIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  HTML_PREVIEW_SANDBOX,
  SLIDES_MSG_SOURCE,
  countSlideSections,
  prepareSlidesDoc,
} from "./codeViewerHelpers";
import { TruncatedBanner } from "./TruncatedBanner";

// Fixed 16:9 stage; the iframe renders at this size and is scaled to fit.
const STAGE_W = 1280;
const STAGE_H = 720;

const PREV_KEYS = new Set(["ArrowLeft", "PageUp"]);
const NEXT_KEYS = new Set(["ArrowRight", "PageDown"]);

export interface SlidesViewerProps {
  content: string;
  truncated?: boolean;
  /** Switches the file viewer to the existing source view. */
  onRequestSourceMode?: () => void;
}

export function SlidesViewer({
  content,
  truncated = false,
  onRequestSourceMode,
}: SlidesViewerProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const total = useMemo(() => countSlideSections(content), [content]);
  const srcDoc = useMemo(() => prepareSlidesDoc(content), [content]);
  const [index, setIndex] = useState(0);
  const [scale, setScale] = useState(0);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const fullscreenSupported = typeof document !== "undefined" && !!document.fullscreenEnabled;

  const current = Math.min(index, Math.max(0, total - 1));
  const step = useCallback(
    (delta: number) => setIndex((i) => Math.max(0, Math.min(total - 1, i + delta))),
    [total],
  );

  const post = useCallback((msg: Record<string, unknown>) => {
    iframeRef.current?.contentWindow?.postMessage({ source: SLIDES_MSG_SOURCE, ...msg }, "*");
  }, []);

  useEffect(() => post({ type: "goto", index: current }), [current, post]);

  // Keys pressed inside the iframe arrive as messages; trust only our iframe.
  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      const win = iframeRef.current?.contentWindow;
      if (!win || e.source !== win || e.data?.source !== SLIDES_MSG_SOURCE) return;
      if (e.data.type !== "key") return;
      if (PREV_KEYS.has(e.data.key)) step(-1);
      else if (NEXT_KEYS.has(e.data.key)) step(1);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [step]);

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
  }, [total]);

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

  if (total === 0) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 p-8 text-center text-ui text-muted-foreground">
        <PresentationIcon className="size-6" />
        <p className="font-medium text-foreground">No slides yet</p>
        <p>Add a top-level &lt;section&gt; for each slide.</p>
        {sourceButton}
      </div>
    );
  }

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
      <div
        ref={stageRef}
        className="relative flex min-h-0 flex-1 items-center justify-center overflow-hidden bg-muted"
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
        <div className="flex items-center gap-1">
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
            <span className="hidden sm:inline">Print / PDF</span>
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
    </div>
  );
}
