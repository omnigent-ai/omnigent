// Slide-deck viewer for `*.slides.html`: one top-level <section> per slide,
// rendered in the same sandboxed srcdoc iframe as the HTML preview. The parent
// only talks to the deck over postMessage (the iframe has an opaque origin).

import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
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
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { fetchFileContent, triggerBrowserDownload } from "@/hooks/useFileContent";
import { DESIGN_SYSTEM_POINTER } from "@/lib/designSystem";
import { isOwnerLevel } from "@/lib/permissionsApi";
import { getSessionSlim } from "@/lib/sessionsApi";
import { cn } from "@/lib/utils";
import {
  DESIGN_KIT_DIR,
  HTML_PREVIEW_SANDBOX,
  SLIDES_EDITABLE_SELECTOR,
  SLIDES_MSG_SOURCE,
  countSlideSections,
  prepareSlidesDoc,
  prepareSlidesExport,
  type KitFile,
} from "./codeViewerHelpers";
import {
  NO_BRANDING,
  dsNotApplied,
  kitNotApplied,
  loadDeckBranding,
  withNotice,
  type DeckBranding,
} from "./deckBranding";
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

// A stalled kit or design-system read must not leave the deck blank. A full
// design system loads more files, so it gets longer once it is found.
export const DESIGN_KIT_TIMEOUT_MS = 2000;
export const DESIGN_SYSTEM_TIMEOUT_MS = 10_000;
const KIT_TIMED_OUT = withNotice(kitNotApplied("design kit timed out"));
const SYSTEM_TIMED_OUT = withNotice(dsNotApplied("design system timed out"));

/** Workspace or absolute file read for the branding loader; a 404 means "no such file". */
async function readKitFile(conversationId: string, path: string): Promise<KitFile | null> {
  try {
    return await fetchFileContent(conversationId, path);
  } catch (e) {
    if (e instanceof Error && e.message.startsWith("404")) return null;
    throw e;
  }
}

async function isSessionOwner(conversationId: string): Promise<boolean> {
  return isOwnerLevel((await getSessionSlim(conversationId)).permissionLevel);
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
}

export function SlidesViewer({
  content,
  truncated = false,
  path: deckPath,
  conversationId,
  onRequestSourceMode,
}: SlidesViewerProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const sourceTotal = useMemo(() => countSlideSections(content), [content]);
  // Hold the deck blank until this session's branding resolves so it never
  // flashes unbranded; branding loaded for another session counts as not loaded.
  const [loaded, setLoaded] = useState<{ id: string; branding: DeckBranding } | null>(null);
  const branding = !conversationId
    ? NO_BRANDING
    : loaded?.id === conversationId
      ? loaded.branding
      : null;
  // Design-system files are read once per session, not on every deck write.
  const systemReads = useRef<{ id: string; files: Map<string, Promise<KitFile | null>> }>(null);
  useEffect(() => {
    if (!conversationId) return;
    if (systemReads.current?.id !== conversationId) {
      systemReads.current = { id: conversationId, files: new Map() };
    }
    const cache = systemReads.current.files;
    const used = new Set<string>();
    let cancelled = false;
    const read = (path: string) => {
      if (path === DESIGN_SYSTEM_POINTER || path.startsWith(`${DESIGN_KIT_DIR}/`)) {
        return readKitFile(conversationId, path);
      }
      if (cancelled) return Promise.reject(new Error("design system load cancelled"));
      used.add(path);
      let file = cache.get(path);
      if (!file) {
        file = readKitFile(conversationId, path);
        file.catch(() => cache.delete(path));
        cache.set(path, file);
      }
      return file;
    };
    const finish = (b: DeckBranding) => {
      if (cancelled) return;
      cancelled = true;
      // Keep only what this deck read, so the cache never outgrows one deck.
      for (const path of cache.keys()) if (!used.has(path)) cache.delete(path);
      setLoaded({ id: conversationId, branding: b });
    };
    let timer = setTimeout(() => finish(KIT_TIMED_OUT), DESIGN_KIT_TIMEOUT_MS);
    void loadDeckBranding(content, {
      read,
      isOwner: () => isSessionOwner(conversationId),
      onDesignSystem: () => {
        clearTimeout(timer);
        timer = setTimeout(() => finish(SYSTEM_TIMED_OUT), DESIGN_SYSTEM_TIMEOUT_MS);
      },
    }).then(finish);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [conversationId, content]);
  const brandingReady = branding !== null;
  const deckContent = branding?.content ?? content;
  const kitStyle = branding?.kitStyle ?? "";
  const systemStyle = branding?.systemStyle ?? "";
  const srcDoc = useMemo(
    () => (brandingReady ? prepareSlidesDoc(deckContent, kitStyle, systemStyle) : ""),
    [deckContent, brandingReady, kitStyle, systemStyle],
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
  // A notice means the kit or design system did not load, so assets would be missing.
  const canExport = brandingReady && !truncated && !branding.notice;
  const downloadHtml = () => {
    const file = deckPath?.split("/").at(-1) ?? "deck.slides.html";
    const name = file.replace(/\.slides\.html$/i, "");
    const html = prepareSlidesExport(deckContent, kitStyle, systemStyle);
    triggerBrowserDownload(new Blob([html], { type: "text/html" }), `${name}.html`);
  };

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
            <Button
              type="button"
              variant="ghost"
              size="sm"
              aria-label="Download HTML"
              title={
                canExport
                  ? "Download HTML"
                  : "Download HTML needs the full deck with its branding loaded"
              }
              disabled={!canExport}
              onClick={downloadHtml}
              className="h-8 gap-1.5 px-2"
            >
              <DownloadIcon className="size-4" />
              <span className="hidden sm:inline">Download HTML</span>
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
