import { createPortal } from "react-dom";
import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
  type RefObject,
  type UIEvent,
} from "react";
import { MessageSquarePlusIcon } from "lucide-react";
import type { Comment } from "@/hooks/useComments";
import { getEmbedRoot } from "@/lib/host";
import type { ActiveSelection } from "./codeViewerHelpers";

export const RENDERED_PREVIEW_ANCHOR_PREFIX = "__OMNIGENT_RENDERED_PREVIEW_ANCHOR__:";
export const RENDERED_PREVIEW_REGION_ATTR = "data-rendered-preview-comment-region";

export interface RenderedPreviewAnchorData {
  surface: string;
  region: string;
  start: number;
  end: number;
  text: string;
}

interface FloatingComment {
  x: number;
  y: number;
  selection: ActiveSelection;
}

interface RenderedPreviewCommentSurfaceProps {
  surface: string;
  rootRef?: RefObject<HTMLDivElement | null>;
  comments?: Comment[];
  activeSelection?: ActiveSelection | null;
  onSetActiveSelection?: (selection: ActiveSelection | null) => void;
  canComment?: boolean;
  onScroll?: (event: UIEvent<HTMLElement>) => void;
  className?: string;
  children: ReactNode;
}

const COMMENT_HIGHLIGHT = "rendered-preview-comment";
const ACTIVE_COMMENT_HIGHLIGHT = "rendered-preview-comment-active";
const EMPTY_COMMENTS: Comment[] = [];
const commentRangesBySurface = new Map<symbol, Range[]>();
const activeRangesBySurface = new Map<symbol, Range[]>();

// React-owned previews opt in by wrapping their rendered DOM with this
// component and marking independently anchored text regions with
// `data-rendered-preview-comment-region`.

function anchorIndex(data: RenderedPreviewAnchorData): number {
  let hash = 2166136261;
  const value = `${data.surface}:${data.region}:${data.start}:${data.end}`;
  for (let i = 0; i < value.length; i++) {
    hash ^= value.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return 1_000_000_000 + ((hash >>> 0) % 800_000_000);
}

export function encodeRenderedPreviewAnchor(data: RenderedPreviewAnchorData): ActiveSelection {
  const start_index = anchorIndex(data);
  return {
    start_index,
    end_index: start_index + Math.min(Math.max(1, data.end - data.start), 1_000_000),
    anchor_content: RENDERED_PREVIEW_ANCHOR_PREFIX + JSON.stringify(data),
  };
}

export function decodeRenderedPreviewAnchor(
  anchorContent: string | null | undefined,
): RenderedPreviewAnchorData | null {
  if (!anchorContent?.startsWith(RENDERED_PREVIEW_ANCHOR_PREFIX)) return null;
  try {
    const data = JSON.parse(
      anchorContent.slice(RENDERED_PREVIEW_ANCHOR_PREFIX.length),
    ) as RenderedPreviewAnchorData;
    if (
      typeof data.surface !== "string" ||
      !data.surface.trim() ||
      typeof data.region !== "string" ||
      !data.region.trim() ||
      !Number.isInteger(data.start) ||
      !Number.isInteger(data.end) ||
      data.start < 0 ||
      data.end <= data.start ||
      typeof data.text !== "string" ||
      !data.text.trim() ||
      data.text.length !== data.end - data.start
    ) {
      return null;
    }
    return data;
  } catch {
    return null;
  }
}

export function isRenderedPreviewAnchor(anchorContent: string | null | undefined): boolean {
  return decodeRenderedPreviewAnchor(anchorContent) !== null;
}

function commentRegion(node: Node, root: HTMLElement): HTMLElement | null {
  const element = node instanceof Element ? node : node.parentElement;
  const region = element?.closest<HTMLElement>(`[${RENDERED_PREVIEW_REGION_ATTR}]`) ?? null;
  return region && root.contains(region) ? region : null;
}

function textOffset(root: HTMLElement, node: Node, offset: number): number | null {
  try {
    const range = document.createRange();
    range.selectNodeContents(root);
    range.setEnd(node, offset);
    return range.toString().length;
  } catch {
    return null;
  }
}

function textRange(root: HTMLElement, start: number, end: number): Range | null {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let position = 0;
  let startPoint: { node: Node; offset: number } | null = null;
  let endPoint: { node: Node; offset: number } | null = null;
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const length = node.nodeValue?.length ?? 0;
    if (!startPoint && start <= position + length) {
      startPoint = { node, offset: Math.max(0, start - position) };
    }
    if (end <= position + length) {
      endPoint = { node, offset: Math.max(0, end - position) };
      break;
    }
    position += length;
  }
  if (!startPoint || !endPoint) return null;
  const range = document.createRange();
  range.setStart(startPoint.node, startPoint.offset);
  range.setEnd(endPoint.node, endPoint.offset);
  return range;
}

export function resolveRenderedPreviewOffsets(
  text: string,
  anchor: Pick<RenderedPreviewAnchorData, "start" | "end" | "text">,
): { start: number; end: number } | null {
  if (text.slice(anchor.start, anchor.end) === anchor.text) {
    return { start: anchor.start, end: anchor.end };
  }
  const searchWindow = 500;
  const windowStart = Math.max(0, anchor.start - searchWindow);
  const windowEnd = Math.min(text.length, anchor.end + searchWindow);
  const nearby = text.indexOf(anchor.text, windowStart);
  const start = nearby !== -1 && nearby <= windowEnd ? nearby : text.indexOf(anchor.text);
  return start === -1 ? null : { start, end: start + anchor.text.length };
}

function findRegion(root: HTMLElement, key: string): HTMLElement | null {
  return (
    Array.from(root.querySelectorAll<HTMLElement>(`[${RENDERED_PREVIEW_REGION_ATTR}]`)).find(
      (region) => region.getAttribute(RENDERED_PREVIEW_REGION_ATTR) === key,
    ) ?? null
  );
}

function sameOffsets(a: ActiveSelection, b: Pick<Comment, "start_index" | "end_index">): boolean {
  return a.start_index === b.start_index && a.end_index === b.end_index;
}

function highlightsSupported(): boolean {
  return typeof CSS !== "undefined" && !!CSS.highlights && typeof Highlight !== "undefined";
}

function syncHighlights(): void {
  if (!highlightsSupported()) return;
  CSS.highlights.set(
    COMMENT_HIGHLIGHT,
    new Highlight(...Array.from(commentRangesBySurface.values()).flat()),
  );
  CSS.highlights.set(
    ACTIVE_COMMENT_HIGHLIGHT,
    new Highlight(...Array.from(activeRangesBySurface.values()).flat()),
  );
}

export function RenderedPreviewCommentSurface({
  surface,
  rootRef,
  comments = EMPTY_COMMENTS,
  activeSelection = null,
  onSetActiveSelection,
  canComment = false,
  onScroll,
  className,
  children,
}: RenderedPreviewCommentSurfaceProps) {
  const internalRootRef = useRef<HTMLDivElement>(null);
  const previewRef = rootRef ?? internalRootRef;
  const highlightKey = useRef(Symbol()).current;
  const [floating, setFloating] = useState<FloatingComment | null>(null);

  useLayoutEffect(() => {
    if (!highlightsSupported()) return;
    const root = previewRef.current;
    if (!root) return;
    const activeRanges: Range[] = [];
    const commentRanges: Range[] = [];

    for (const comment of comments) {
      const anchor = decodeRenderedPreviewAnchor(comment.anchor_content);
      if (!anchor || anchor.surface !== surface) continue;
      const region = findRegion(root, anchor.region);
      const offsets = region && resolveRenderedPreviewOffsets(region.textContent ?? "", anchor);
      const range = region && offsets && textRange(region, offsets.start, offsets.end);
      if (!range) continue;
      (activeSelection && sameOffsets(activeSelection, comment)
        ? activeRanges
        : commentRanges
      ).push(range);
    }

    const activeAnchor = decodeRenderedPreviewAnchor(activeSelection?.anchor_content);
    if (
      activeAnchor?.surface === surface &&
      !comments.some((comment) => sameOffsets(activeSelection!, comment))
    ) {
      const region = findRegion(root, activeAnchor.region);
      const offsets =
        region && resolveRenderedPreviewOffsets(region.textContent ?? "", activeAnchor);
      const range = region && offsets && textRange(region, offsets.start, offsets.end);
      if (range) activeRanges.push(range);
    }

    commentRangesBySurface.set(highlightKey, commentRanges);
    activeRangesBySurface.set(highlightKey, activeRanges);
    syncHighlights();
    return () => {
      commentRangesBySurface.delete(highlightKey);
      activeRangesBySurface.delete(highlightKey);
      syncHighlights();
    };
  }, [activeSelection, children, comments, highlightKey, previewRef, surface]);

  useEffect(() => {
    const anchor = decodeRenderedPreviewAnchor(activeSelection?.anchor_content);
    const root = previewRef.current;
    if (!anchor || anchor.surface !== surface || !root) return;
    findRegion(root, anchor.region)?.scrollIntoView?.({ block: "center" });
    const selection = window.getSelection();
    if (selection && !selection.isCollapsed) selection.removeAllRanges();
  }, [activeSelection, previewRef, surface]);

  useEffect(() => {
    const dismiss = (event: MouseEvent) => {
      if (!(event.target instanceof Element && event.target.closest("[data-add-comment-btn]"))) {
        setFloating(null);
      }
    };
    document.addEventListener("mousedown", dismiss);
    return () => document.removeEventListener("mousedown", dismiss);
  }, []);

  useEffect(() => {
    if (!canComment) setFloating(null);
  }, [canComment]);

  const handleMouseUp = (event: ReactMouseEvent<HTMLDivElement>) => {
    const root = previewRef.current;
    const selection = window.getSelection();
    if (!root || !selection || selection.rangeCount === 0 || !onSetActiveSelection) {
      setFloating(null);
      return;
    }
    const range = selection.getRangeAt(0);
    if (!root.contains(range.commonAncestorContainer)) {
      setFloating(null);
      return;
    }

    const startRegion = commentRegion(range.startContainer, root);
    const endRegion = commentRegion(range.endContainer, root);
    if (!startRegion || startRegion !== endRegion) {
      setFloating(null);
      return;
    }
    const region = startRegion.getAttribute(RENDERED_PREVIEW_REGION_ATTR);
    const start = textOffset(startRegion, range.startContainer, range.startOffset);
    const end = textOffset(startRegion, range.endContainer, range.endOffset);
    if (!region || start == null || end == null) {
      setFloating(null);
      return;
    }

    if (selection.isCollapsed) {
      const comment = comments.find((candidate) => {
        const anchor = decodeRenderedPreviewAnchor(candidate.anchor_content);
        const offsets =
          anchor && resolveRenderedPreviewOffsets(startRegion.textContent ?? "", anchor);
        return (
          anchor?.surface === surface &&
          anchor.region === region &&
          offsets !== null &&
          offsets.start <= start &&
          start < offsets.end
        );
      });
      if (comment) {
        onSetActiveSelection({
          start_index: comment.start_index,
          end_index: comment.end_index,
          anchor_content: comment.anchor_content ?? "",
        });
      } else if (!(
        event.target instanceof Element && event.target.closest("[data-add-comment-btn]")
      )) {
        onSetActiveSelection(null);
      }
      setFloating(null);
      return;
    }

    const text = range.toString();
    if (!canComment || !text.trim()) {
      setFloating(null);
      return;
    }
    const next = encodeRenderedPreviewAnchor({ surface, region, start, end, text });
    const existing = comments.find((comment) => {
      const anchor = decodeRenderedPreviewAnchor(comment.anchor_content);
      const offsets =
        anchor && resolveRenderedPreviewOffsets(startRegion.textContent ?? "", anchor);
      return (
        anchor?.surface === surface &&
        anchor.region === region &&
        offsets?.start === start &&
        offsets.end === end
      );
    });
    if (existing) {
      onSetActiveSelection({
        start_index: existing.start_index,
        end_index: existing.end_index,
        anchor_content: existing.anchor_content ?? "",
      });
      setFloating(null);
      return;
    }

    const rect = range.getClientRects?.()[0] ?? range.getBoundingClientRect?.();
    setFloating({ x: rect?.left ?? 8, y: (rect?.top ?? 8) - 6, selection: next });
  };

  return (
    <>
      <div
        ref={previewRef}
        data-preview-scroll
        onMouseUp={handleMouseUp}
        onScroll={(event) => {
          setFloating(null);
          onScroll?.(event);
        }}
        className={className}
      >
        {children}
      </div>
      {floating &&
        canComment &&
        onSetActiveSelection &&
        createPortal(
          <button
            data-add-comment-btn
            type="button"
            className="fixed z-50 flex items-center gap-1.5 rounded-md border border-border bg-popover backdrop-blur-xl backdrop-saturate-150 px-2.5 py-1 text-sm font-medium text-foreground shadow-md hover:bg-secondary transition-colors"
            style={{ left: floating.x, top: floating.y, transform: "translateY(-100%)" }}
            onMouseDown={(event) => event.preventDefault()}
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
    </>
  );
}
