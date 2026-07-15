// Render a PDF file inline via react-pdf (pdf.js). Mirrors ImageViewer's blob
// lifecycle: build an object URL from the file response, revoke on cleanup, and
// treat a truncated (partial, unparseable) byte stream as an error.
//
// The pages render in a scrollable column fit to the container width, with a
// small toolbar for zoom and page count. This module is lazy-loaded from
// CodeViewer, so the react-pdf/pdf.js bundle and its worker only load when a PDF
// is actually opened.

import { useEffect, useMemo, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import "react-pdf/dist/Page/AnnotationLayer.css";
import "react-pdf/dist/Page/TextLayer.css";
import { MinusIcon, PlusIcon } from "lucide-react";
import { fileContentToBlob, type FileContentResponse } from "@/hooks/useFileContent";
import { Button } from "@/components/ui/button";
import { TruncatedBanner } from "./TruncatedBanner";

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

export function PdfViewer({ data }: { data: FileContentResponse }) {
  const [url, setUrl] = useState<string | null>(null);
  const [numPages, setNumPages] = useState(0);
  const [errored, setErrored] = useState(false);
  const [scale, setScale] = useState(1);
  const containerRef = useRef<HTMLDivElement>(null);
  const [containerWidth, setContainerWidth] = useState<number | null>(null);

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
          {/* The percentage doubles as the reset control — click it to return
              to 100%. */}
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

      <div ref={containerRef} className="min-h-0 flex-1 overflow-auto bg-muted/30 p-4">
        {url && (
          <Document
            file={url}
            onLoadSuccess={(doc) => setNumPages(doc.numPages)}
            onLoadError={() => setErrored(true)}
            loading={centered("Loading PDF…")}
            error={centered("Unable to render PDF.", "error")}
            className="flex flex-col items-center gap-4"
          >
            {Array.from({ length: numPages }, (_, i) => (
              <Page
                key={i + 1}
                pageNumber={i + 1}
                width={pageWidth}
                className="shadow-md"
                loading=""
              />
            ))}
          </Document>
        )}
      </div>
    </div>
  );
}
