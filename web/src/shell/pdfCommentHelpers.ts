// PDF comment anchoring: encode page-relative highlight geometry and selected
// text into the existing Comment fields. Highlights are browser-only overlays
// (not written into the PDF bytes); anchor_content carries the geometry the
// viewer needs to repaint them after reload.

import type { Comment } from "@/hooks/useComments";
import type { ActiveSelection } from "./codeViewerHelpers";

/** Prefix that marks anchor_content as a PDF geometry payload, not raw text. */
export const PDF_ANCHOR_PREFIX = "__pdf__";

/** Rectangle relative to the page container (0–1 fractions). */
export interface PdfNormalizedRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface PdfAnchorData {
  page: number;
  rects: PdfNormalizedRect[];
  text: string;
}

export function isPdfAnchor(anchorContent: string | null | undefined): boolean {
  return !!anchorContent?.startsWith(PDF_ANCHOR_PREFIX);
}

export function decodePdfAnchor(anchorContent: string | null | undefined): PdfAnchorData | null {
  if (!isPdfAnchor(anchorContent)) return null;
  try {
    return JSON.parse(anchorContent!.slice(PDF_ANCHOR_PREFIX.length)) as PdfAnchorData;
  } catch {
    return null;
  }
}

/** Human-readable anchor text for the comments panel and agent messages. */
export function displayAnchorContent(anchorContent: string | null | undefined): string {
  const decoded = decodePdfAnchor(anchorContent);
  if (decoded) return decoded.text;
  return anchorContent?.trim() ?? "";
}

function computePdfStartIndex(page: number, rects: PdfNormalizedRect[]): number {
  const first = rects[0];
  if (!first) return (page - 1) * 1_000_000;
  const xPart = Math.round(first.x * 10_000);
  const yPart = Math.round(first.y * 10_000);
  return (page - 1) * 1_000_000 + yPart * 10_000 + xPart;
}

/** Build an ActiveSelection from a PDF text-layer selection. */
export function encodePdfAnchor(
  page: number,
  rects: PdfNormalizedRect[],
  text: string,
): ActiveSelection {
  const data: PdfAnchorData = { page, rects, text };
  const start_index = computePdfStartIndex(page, rects);
  return {
    start_index,
    end_index: start_index + text.length,
    anchor_content: PDF_ANCHOR_PREFIX + JSON.stringify(data),
  };
}

export function getSelectionNormalizedRects(
  range: Range,
  pageEl: HTMLElement,
): PdfNormalizedRect[] {
  const pageRect = pageEl.getBoundingClientRect();
  if (pageRect.width <= 0 || pageRect.height <= 0) return [];
  const rects: PdfNormalizedRect[] = [];
  for (let i = 0; i < range.getClientRects().length; i++) {
    const r = range.getClientRects()[i]!;
    if (r.width <= 0 || r.height <= 0) continue;
    rects.push({
      x: (r.left - pageRect.left) / pageRect.width,
      y: (r.top - pageRect.top) / pageRect.height,
      w: r.width / pageRect.width,
      h: r.height / pageRect.height,
    });
  }
  return rects;
}

/** Walk up from a selection node to the react-pdf page root (`[data-page-number]`). */
export function findPageElement(node: Node, container: HTMLElement): HTMLElement | null {
  let el: Node | null = node.nodeType === Node.TEXT_NODE ? node.parentElement : (node as Element);
  while (el && el !== container) {
    if (el instanceof HTMLElement && el.hasAttribute("data-page-number")) return el;
    el = el.parentElement;
  }
  return null;
}

export function getPageNumber(pageEl: HTMLElement): number {
  return parseInt(pageEl.getAttribute("data-page-number") ?? "1", 10);
}

export function commentsMatchOffsets(
  a: { start_index: number; end_index: number },
  b: { start_index: number; end_index: number },
): boolean {
  return a.start_index === b.start_index && a.end_index === b.end_index;
}

/** Collect highlight rects for a comment or pending selection on a given page. */
export function highlightRectsForPage(
  page: number,
  comments: Comment[],
  activeSelection: ActiveSelection | null,
): Array<{ key: string; rects: PdfNormalizedRect[]; active: boolean; comment?: Comment }> {
  const out: Array<{
    key: string;
    rects: PdfNormalizedRect[];
    active: boolean;
    comment?: Comment;
  }> = [];

  for (const c of comments) {
    const anchor = decodePdfAnchor(c.anchor_content);
    if (!anchor || anchor.page !== page) continue;
    const active = activeSelection != null && commentsMatchOffsets(activeSelection, c);
    out.push({ key: c.id, rects: anchor.rects, active, comment: c });
  }

  if (activeSelection) {
    const pending = decodePdfAnchor(activeSelection.anchor_content);
    const alreadySaved = comments.some((c) => commentsMatchOffsets(activeSelection, c));
    if (pending && pending.page === page && !alreadySaved) {
      out.push({ key: "pending", rects: pending.rects, active: true });
    }
  }

  return out;
}
