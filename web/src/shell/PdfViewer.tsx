// Render a PDF file inline via react-pdf (pdf.js). Mirrors ImageViewer's blob
// lifecycle: build an object URL from the file response, revoke on cleanup, and
// treat a truncated (partial, unparseable) byte stream as an error.
//
// The pages render in a scrollable column fit to the container width, with a
// small toolbar for zoom and page count. This module is lazy-loaded from
// CodeViewer, so the react-pdf/pdf.js bundle and its worker only load when a PDF
// is actually opened.
//
// Comment support: text-layer selections open the comments panel (same flow as
// code/markdown/HTML). Saved comments paint browser-only highlight overlays on
// top of the rendered pages — nothing is written into the PDF bytes.

import { createPortal } from "react-dom";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import "react-pdf/dist/Page/AnnotationLayer.css";
import "react-pdf/dist/Page/TextLayer.css";
import { MessageSquarePlusIcon, MinusIcon, PlusIcon } from "lucide-react";
import { fileContentToBlob, type FileContentResponse } from "@/hooks/useFileContent";
import { type Comment } from "@/hooks/useComments";
import { useCanEdit } from "@/hooks/usePermissions";
import { Button } from "@/components/ui/button";
import { getEmbedRoot } from "@/lib/host";
import { cn } from "@/lib/utils";
import { type ActiveSelection } from "./codeViewerHelpers";
import {
  commentsMatchOffsets,
  decodePdfAnchor,
  encodePdfAnchor,
  findPageElement,
  getPageNumber,
  getSelectionNormalizedRects,
  highlightRectsForPage,
} from "./pdfCommentHelpers";
import { TruncatedBanner } from "./TruncatedBanner";
import "./pdfViewer.css";

// Point pdf.js at its worker. `new URL(..., import.meta.url)` lets Vite fingerprint
// and serve the worker as an asset; running it at module scope is fine because the
// module itself is lazy-loaded (no cost until a PDF opens).
pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  "pdfjs-dist/build/pdf.worker.min.mjs",
  import.meta.url,
).toString();

const MIN_SCALE = 0.5;
const MAX_SCALE = 3;
const SCALE_STEP = 0.25;

interface FloatingAnchor {
  x: number;
  y: number;
  selection: ActiveSelection;
}

export interface PdfViewerProps {
  data: FileContentResponse;
  conversationId: string;
  comments?: Comment[];
  activeSelection?: ActiveSelection | null;
  onSetActiveSelection?: (sel: ActiveSelection | null) => void;
}

function centered(message: string, tone: "muted" | "error" = "muted") {
  return (
    <div
      className={
        tone === "error"
          ? "flex items-center justify-center p-8 text-destructive text-sm"
          : "flex items-center justify-center p-8 text-muted-foreground text-sm"
      }
    >
      {message}
    </div>
  );
}

function PdfCommentHighlights({
  page,
  comments,
  activeSelection,
  onCommentClick,
}: {
  page: number;
  comments: Comment[];
  activeSelection: ActiveSelection | null;
  onCommentClick: (comment: Comment) => void;
}) {
  const highlights = highlightRectsForPage(page, comments, activeSelection);
  if (highlights.length === 0) return null;

  return (
    <div className="pdf-comment-layer" aria-hidden>
      {highlights.map((h) =>
        h.rects.map((rect, i) => (
          <div
            key={`${h.key}-${i}`}
            className={cn("pdf-comment", h.active && "pdf-comment-active")}
            style={{
              left: `${rect.x * 100}%`,
              top: `${rect.y * 100}%`,
              width: `${rect.w * 100}%`,
              height: `${rect.h * 100}%`,
            }}
            onClick={(e) => {
              e.stopPropagation();
              if (h.comment) onCommentClick(h.comment);
            }}
          />
        )),
      )}
    </div>
  );
}

export function PdfViewer({
  data,
  conversationId,
  comments = [],
  activeSelection = null,
  onSetActiveSelection,
}: PdfViewerProps) {
  const canEdit = useCanEdit(conversationId);
  const [url, setUrl] = useState<string | null>(null);
  const [numPages, setNumPages] = useState(0);
  const [errored, setErrored] = useState(false);
  const [scale, setScale] = useState(1);
  const containerRef = useRef<HTMLDivElement>(null);
  const [containerWidth, setContainerWidth] = useState<number | null>(null);
  const [floating, setFloating] = useState<FloatingAnchor | null>(null);

  const commentsRef = useRef(comments);
  commentsRef.current = comments;
  const onSetActiveSelectionRef = useRef(onSetActiveSelection);
  onSetActiveSelectionRef.current = onSetActiveSelection;
  const activeSelectionRef = useRef(activeSelection);
  activeSelectionRef.current = activeSelection;

  // Build/revoke the object URL when the file changes. A truncated PDF is a
  // partial byte stream that pdf.js can't parse, so skip the blob and show the
  // error/banner UI instead of flashing a failed render.
  useEffect(() => {
    if (data.truncated) {
      setUrl(null);
      setErrored(true);
      return;
    }
    setErrored(false);
    setNumPages(0);
    const objectUrl = URL.createObjectURL(fileContentToBlob(data));
    setUrl(objectUrl);
    return () => URL.revokeObjectURL(objectUrl);
  }, [data]);

  // Measure the container so pages fit its width (minus padding) at scale 1.
  useEffect(() => {
    const el = containerRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const measure = () => setContainerWidth(el.clientWidth);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // 32px accounts for the horizontal padding around the page column.
  const pageWidth = useMemo(
    () => (containerWidth ? Math.max(0, containerWidth - 32) * scale : undefined),
    [containerWidth, scale],
  );

  const zoomOut = () => setScale((s) => Math.max(MIN_SCALE, s - SCALE_STEP));
  const zoomIn = () => setScale((s) => Math.min(MAX_SCALE, s + SCALE_STEP));
  const resetZoom = () => setScale(1);

  const handleCommentClick = useCallback((comment: Comment) => {
    onSetActiveSelectionRef.current?.({
      start_index: comment.start_index,
      end_index: comment.end_index,
      anchor_content: comment.anchor_content ?? "",
    });
    setFloating(null);
  }, []);

  // Scroll the page containing the active comment into view.
  useEffect(() => {
    if (!activeSelection) return;
    const anchor = decodePdfAnchor(activeSelection.anchor_content);
    if (!anchor) return;
    const pageEl = containerRef.current?.querySelector(
      `[data-page-number="${anchor.page}"]`,
    ) as HTMLElement | null;
    pageEl?.scrollIntoView({ block: "center" });
  }, [activeSelection]);

  // Drop the native selection once our overlay covers it — otherwise the app
  // theme's ::selection highlight stacks underneath and looks misaligned.
  useEffect(() => {
    if (!activeSelection) return;
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed) sel.removeAllRanges();
  }, [activeSelection]);

  // Capture text-layer selections and show the floating "Add comment" button.
  useEffect(() => {
    const container = containerRef.current;
    if (!container || !onSetActiveSelection) return;

    const handleMouseUp = (e: MouseEvent) => {
      const sel = window.getSelection();
      if (!sel || sel.rangeCount === 0) return;
      const range = sel.getRangeAt(0);

      if (sel.isCollapsed) {
        if (!container.contains(range.commonAncestorContainer)) return;
        // Highlight overlays activate comments on click; don't clear here.
        if ((e.target as Element).closest(".pdf-comment")) return;
        onSetActiveSelectionRef.current?.(null);
        setFloating(null);
        return;
      }

      if (!canEdit) return;
      if (!container.contains(range.commonAncestorContainer)) return;
      const text = sel.toString();
      if (!text.trim()) return;

      const pageEl = findPageElement(range.commonAncestorContainer, container);
      if (!pageEl) return;
      const rects = getSelectionNormalizedRects(range, pageEl);
      if (rects.length === 0) return;

      const page = getPageNumber(pageEl);
      const selection = encodePdfAnchor(page, rects, text);

      const existing = commentsRef.current.find((c) => commentsMatchOffsets(selection, c));
      if (existing) {
        onSetActiveSelectionRef.current?.({
          start_index: existing.start_index,
          end_index: existing.end_index,
          anchor_content: existing.anchor_content ?? "",
        });
        setFloating(null);
        return;
      }

      const firstRect = range.getClientRects()[0] ?? range.getBoundingClientRect();
      setFloating({
        x: firstRect.left,
        y: firstRect.top - 6,
        selection,
      });
    };

    container.addEventListener("mouseup", handleMouseUp);
    return () => container.removeEventListener("mouseup", handleMouseUp);
  }, [canEdit, onSetActiveSelection]);

  // Dismiss the floating button on mousedown outside or scroll.
  useEffect(() => {
    const handleMouseDown = (e: MouseEvent) => {
      if (!(e.target as HTMLElement).closest("[data-add-comment-btn]")) setFloating(null);
    };
    const handleScroll = () => setFloating(null);
    document.addEventListener("mousedown", handleMouseDown);
    window.addEventListener("scroll", handleScroll, true);
    return () => {
      document.removeEventListener("mousedown", handleMouseDown);
      window.removeEventListener("scroll", handleScroll, true);
    };
  }, []);

  useEffect(() => {
    setFloating(null);
  }, [scale, data]);

  if (errored) {
    const body = centered(
      data.truncated
        ? "PDF is too large to preview (truncated by the server)."
        : "Unable to render PDF.",
    );
    if (!data.truncated) return body;
    return (
      <div className="flex h-full flex-col">
        <TruncatedBanner />
        {body}
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      {/* Toolbar: page count + zoom controls. */}
      <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border px-4 py-1.5">
        <span className="text-xs text-muted-foreground tabular-nums">
          {numPages > 0 ? `${numPages} page${numPages === 1 ? "" : "s"}` : ""}
        </span>
        <div className="flex items-center gap-1">
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Zoom out"
            disabled={scale <= MIN_SCALE}
            onClick={zoomOut}
          >
            <MinusIcon className="size-4" />
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            aria-label="Reset zoom"
            title="Reset zoom"
            disabled={scale === 1}
            onClick={resetZoom}
            className="w-12 tabular-nums text-muted-foreground"
          >
            {Math.round(scale * 100)}%
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Zoom in"
            disabled={scale >= MAX_SCALE}
            onClick={zoomIn}
          >
            <PlusIcon className="size-4" />
          </Button>
        </div>
      </div>

      <div ref={containerRef} className="pdf-viewer min-h-0 flex-1 overflow-auto bg-muted/30 p-4">
        {url && (
          <Document
            file={url}
            onLoadSuccess={(doc) => setNumPages(doc.numPages)}
            onLoadError={() => setErrored(true)}
            loading={centered("Loading PDF…")}
            error={centered("Unable to render PDF.", "error")}
            className="flex flex-col items-center gap-4"
          >
            {Array.from({ length: numPages }, (_, i) => {
              const pageNumber = i + 1;
              return (
                <Page
                  key={pageNumber}
                  pageNumber={pageNumber}
                  width={pageWidth}
                  className="shadow-md"
                  loading=""
                >
                  <PdfCommentHighlights
                    page={pageNumber}
                    comments={comments}
                    activeSelection={activeSelection}
                    onCommentClick={handleCommentClick}
                  />
                </Page>
              );
            })}
          </Document>
        )}
      </div>

      {floating &&
        canEdit &&
        onSetActiveSelection &&
        createPortal(
          <button
            data-add-comment-btn
            type="button"
            className="fixed z-50 flex items-center gap-1.5 rounded-md border border-border bg-popover backdrop-blur-xl backdrop-saturate-150 px-2.5 py-1 text-xs font-medium text-foreground shadow-md hover:bg-secondary transition-colors"
            style={{ left: floating.x, top: floating.y, transform: "translateY(-100%)" }}
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => {
              onSetActiveSelection(floating.selection);
              setFloating(null);
            }}
          >
            <MessageSquarePlusIcon className="size-3.5" />
            Add comment
          </button>,
          getEmbedRoot() ?? document.body,
        )}
    </div>
  );
}
