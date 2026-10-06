// Slide-deck viewer for `*.slides.html`: one top-level <section> per slide,
// rendered in the same sandboxed srcdoc iframe as the HTML preview. The parent
// only talks to the deck over postMessage (the iframe has an opaque origin).

import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  CodeIcon,
  DownloadIcon,
  Maximize2Icon,
  Minimize2Icon,
  PaletteIcon,
  PresentationIcon,
  PrinterIcon,
  TriangleAlertIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { triggerBrowserDownload } from "@/hooks/useFileContent";
import type { BrandWarning } from "@/lib/brandRules";
import { cn } from "@/lib/utils";
import type { Comment } from "@/hooks/useComments";
import {
  HTML_PREVIEW_SANDBOX,
  SLIDES_EDITABLE_SELECTOR,
  SLIDES_MSG_SOURCE,
  type ActiveSelection,
  countSlideSections,
  prepareSlidesDoc,
  prepareSlidesExport,
} from "./codeViewerHelpers";
import { useBrandWarnings, useDesignBranding, useFullscreen } from "./designViewer";
import { appendCommentBridge, slideIndexForSourceOffset } from "./htmlCommentBridge";
import { TruncatedBanner } from "./TruncatedBanner";
import { useHtmlCommentBridge } from "./useHtmlCommentBridge";

export { DESIGN_KIT_TIMEOUT_MS, DESIGN_SYSTEM_TIMEOUT_MS } from "./designViewer";

const EMPTY_COMMENTS: Comment[] = [];
const noopSetActiveSelection = (_sel: ActiveSelection | null) => {};

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

/** Filename for Download HTML; odd paths that strip to nothing become `deck.html`. */
export function slidesExportFilename(path?: string | null): string {
  const file = path?.split("/").at(-1) || "deck.slides.html";
  const base = file
    .replace(/\.slides\.html$/i, "")
    .trim()
    .replace(/^\.+|\.+$/g, "");
  return `${base || "deck"}.html`;
}

/** Toolbar badge for raw values outside a full design system; never blocks the deck. */
function BrandWarnings({ warnings }: { warnings: BrandWarning[] }) {
  const label = `${warnings.length} brand warning${warnings.length === 1 ? "" : "s"}`;
  const headingId = useId();
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          aria-label={label}
          title={label}
          className="h-8 gap-1.5 px-2"
        >
          <TriangleAlertIcon className="size-4 text-warning" />
          <span className="hidden sm:inline">{label}</span>
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80" aria-labelledby={headingId}>
        <p id={headingId} className="font-medium text-foreground">
          Values outside the design system
        </p>
        <ul className="flex max-h-64 flex-col gap-1 overflow-auto">
          {warnings.map((w) => (
            <li key={w.value} className="flex min-w-0 items-baseline gap-2">
              <code className="shrink-0 text-foreground">{w.value}</code>
              <span className="shrink-0">{w.property}</span>
              <span className="truncate text-muted-foreground" title={w.where}>
                {w.where}
              </span>
            </li>
          ))}
        </ul>
      </PopoverContent>
    </Popover>
  );
}

export interface SlidesViewerProps {
  content: string;
  truncated?: boolean;
  /** The deck's file path; names the HTML download. */
  path?: string;
  /** Session whose workspace may hold `.omnigent/design-kit/`. */
  conversationId?: string;
  /** Switches the file viewer to the existing source view. */
  onRequestSourceMode?: () => void;
  comments?: Comment[];
  activeSelection?: ActiveSelection | null;
  onSetActiveSelection?: (sel: ActiveSelection | null) => void;
}

export function SlidesViewer({
  content,
  truncated = false,
  path: deckPath,
  conversationId,
  onRequestSourceMode,
  comments = EMPTY_COMMENTS,
  activeSelection = null,
  onSetActiveSelection = noopSetActiveSelection,
}: SlidesViewerProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const sourceTotal = useMemo(() => countSlideSections(content), [content]);
  const branding = useDesignBranding(conversationId, content);
  const brandWarnings = useBrandWarnings(conversationId, content, branding?.systemPath ?? null);
  const brandingReady = branding !== null;
  const deckContent = branding?.content ?? content;
  const kitStyle = branding?.kitStyle ?? "";
  const systemStyle = branding?.systemStyle ?? "";
  const preparedDoc = useMemo(
    () => (brandingReady ? prepareSlidesDoc(deckContent, kitStyle, systemStyle) : ""),
    [deckContent, brandingReady, kitStyle, systemStyle],
  );
  const [scale, setScale] = useState(0);
  const { nonce, iframeRef, addCommentPortal } = useHtmlCommentBridge({
    conversationId: conversationId ?? "",
    content,
    docKey: preparedDoc,
    comments,
    activeSelection,
    onSetActiveSelection,
    scale,
  });
  // Bridge is preview-only; Download HTML keeps prepareSlidesExport without it.
  const srcDoc = useMemo(
    () => (preparedDoc ? appendCommentBridge(preparedDoc, nonce) : ""),
    [preparedDoc, nonce],
  );
  // The iframe's runtime count wins once it reports for the current document.
  const [runtime, setRuntime] = useState<{ srcDoc: string; total: number } | null>(null);
  const total = Math.min(runtime?.srcDoc === srcDoc ? runtime.total : sourceTotal, MAX_SLIDE_COUNT);
  const [index, setIndex] = useState(0);
  const {
    isFullscreen,
    supported: fullscreenSupported,
    toggle: toggleFullscreen,
  } = useFullscreen(rootRef);

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

  const post = useCallback(
    (msg: Record<string, unknown>) => {
      iframeRef.current?.contentWindow?.postMessage({ source: SLIDES_MSG_SOURCE, ...msg }, "*");
    },
    [iframeRef],
  );

  useEffect(() => post({ type: "goto", index: current }), [current, post]);

  // Activate a comment on another slide by jumping to the section that holds it.
  useEffect(() => {
    if (!activeSelection) return;
    const slide = slideIndexForSourceOffset(content, activeSelection.start_index);
    if (slide !== null && slide !== current) setIndex(slide);
  }, [activeSelection, content, current]);

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
  }, [step, srcDoc, iframeRef]);

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
  // A notice means the kit or design system did not load, so assets would be missing.
  const canExport = !empty && brandingReady && !truncated && !branding.notice;
  const exportTitle = empty
    ? "Download HTML needs at least one slide"
    : canExport
      ? "Download HTML"
      : "Download HTML needs the full deck with its branding loaded";
  const downloadHtml = () => {
    if (!canExport) return;
    const html = prepareSlidesExport(deckContent, kitStyle, systemStyle);
    triggerBrowserDownload(new Blob([html], { type: "text/html" }), slidesExportFilename(deckPath));
  };
  const downloadButton = (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      aria-label="Download HTML"
      title={exportTitle}
      disabled={!canExport}
      onClick={downloadHtml}
      className="h-8 gap-1.5 px-2"
    >
      <DownloadIcon className="size-4" />
      <span className="hidden sm:inline">Download HTML</span>
    </Button>
  );

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
      {branding?.notice && (
        <div
          role="status"
          className="shrink-0 truncate border-b border-border bg-muted px-3 py-1 text-ui text-muted-foreground"
          title={branding.notice}
        >
          {branding.notice}
        </div>
      )}
      {empty && (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 p-8 text-center text-ui text-muted-foreground">
          <PresentationIcon className="size-6" />
          <p className="font-medium text-foreground">No slides yet</p>
          <p>Add a top-level &lt;section&gt; for each slide.</p>
          <div className="flex items-center gap-1">
            {downloadButton}
            {sourceButton}
          </div>
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
          {/* Changing srcdoc on a live iframe adds a browser history entry; a new
              iframe per document loads without one. */}
          <iframe
            key={srcDoc}
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
            {branding?.badge && (
              <span
                className="flex min-w-0 items-center gap-1 px-1"
                title={`${branding.badge.kind === "kit" ? "Design kit" : "Design system"}: ${branding.badge.name}`}
              >
                <PaletteIcon className="size-3.5 shrink-0" aria-hidden />
                <span className="max-w-32 truncate max-sm:sr-only">{branding.badge.name}</span>
              </span>
            )}
            {!!brandWarnings?.length && <BrandWarnings warnings={brandWarnings} />}
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
            {downloadButton}
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
      {addCommentPortal}
    </div>
  );
}
